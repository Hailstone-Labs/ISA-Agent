"""Embedded LanceDB semantic memory store, BM25 keyword search, and Reciprocal Rank Fusion (RRF)."""

from __future__ import annotations

import logging
from pathlib import Path
import re
import lancedb
from pydantic import BaseModel
from rank_bm25 import BM25Plus
from config.settings import ISASettings, get_settings
from src.memory.ingest import (
    EmbedderProtocol,
    LocalEmbedder,
    MarkdownHeaderTextSplitter,
    discover_vault_markdown_files,
)

logger = logging.getLogger("isa.memory.vector_db")

TOKEN_REGEX = re.compile(r"\w+", re.UNICODE)


def tokenize_for_bm25(text: str) -> list[str]:
    """Tokenize text into lowercase alphanumeric tokens for BM25 keyword scoring."""
    return [tok.lower() for tok in TOKEN_REGEX.findall(text)]


class VaultChunkRecord(BaseModel):
    """Schema representation for a Markdown header-split chunk stored in LanceDB."""

    chunk_id: str
    file_path: str
    heading_hierarchy: str
    text: str
    vector: list[float]


def reciprocal_rank_fusion(
    ranked_lists: list[list[str]],
    k: int = 60,
) -> list[tuple[str, float]]:
    """Compute Reciprocal Rank Fusion (RRF) score across dense and BM25 result lists.

    Formula: RRF_score(d) = sum_{r in R} 1 / (k + rank_r(d))
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank_idx, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank_idx)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


class LocalVectorStore:
    """Serverless LanceDB manager with Hybrid Search (Dense Vector + BM25 + RRF)."""

    TABLE_NAME = "vault_chunks"

    def __init__(self, settings: ISASettings | None = None) -> None:
        self.settings = settings or get_settings()
        self.uri: Path = self.settings.lancedb_uri
        self.uri.mkdir(parents=True, exist_ok=True)
        self.db = lancedb.connect(str(self.uri))

    def list_tables(self) -> list[str]:
        """Return existing table names in the embedded LanceDB instance."""
        res = self.db.list_tables()
        return list(res.tables) if hasattr(res, "tables") else list(res)

    def upsert_chunks(self, records: list[VaultChunkRecord]) -> int:
        """Create or overwrite the vault_chunks table with the given embedded records."""
        if not records:
            return 0
        payload = [rec.model_dump() for rec in records]
        self.db.create_table(self.TABLE_NAME, data=payload, mode="overwrite")
        return len(payload)

    def get_all_chunks(self) -> list[dict]:
        """Return all stored chunk records from LanceDB."""
        if self.TABLE_NAME not in self.list_tables():
            return []
        table = self.db.open_table(self.TABLE_NAME)
        return table.to_arrow().to_pylist()

    def search_dense(self, query_vector: list[float], limit: int = 10) -> list[dict]:
        """Execute dense semantic vector search via LanceDB."""
        if self.TABLE_NAME not in self.list_tables():
            return []
        table = self.db.open_table(self.TABLE_NAME)
        return table.search(query_vector).limit(limit).to_list()

    def search_bm25(self, query: str, limit: int = 10) -> list[dict]:
        """Execute sparse exact-keyword BM25 search over stored chunks."""
        all_chunks = self.get_all_chunks()
        if not all_chunks:
            return []

        query_tokens = tokenize_for_bm25(query)
        if not query_tokens:
            return []

        corpus_tokens = [
            tokenize_for_bm25(
                f"{c['file_path']} {c['heading_hierarchy']} {c['text']}"
            )
            for c in all_chunks
        ]
        bm25 = BM25Plus(corpus_tokens)
        scores = bm25.get_scores(query_tokens)

        # Require at least one query token to appear in the document
        query_token_set = set(query_tokens)
        scored_indices = sorted(
            [
                idx
                for idx, doc_tokens in enumerate(corpus_tokens)
                if query_token_set.intersection(doc_tokens)
            ],
            key=lambda idx: scores[idx],
            reverse=True,
        )
        results: list[dict] = []
        for idx in scored_indices[:limit]:
            if scores[idx] > 0:
                item = dict(all_chunks[idx])
                item["bm25_score"] = float(scores[idx])
                results.append(item)
        return results

    def hybrid_search(
        self,
        query: str,
        embedder: EmbedderProtocol,
        limit: int = 5,
        rrf_k: int | None = None,
    ) -> list[dict]:
        """Execute Hybrid Search (Dense Vector + BM25) fused with Reciprocal Rank Fusion (RRF)."""
        if self.TABLE_NAME not in self.list_tables():
            return []

        k_val = rrf_k if rrf_k is not None else self.settings.rrf_k
        candidate_pool = max(limit * 3, 10)

        query_vec = embedder.embed_query(query)
        dense_hits = self.search_dense(query_vec, limit=candidate_pool)
        bm25_hits = self.search_bm25(query, limit=candidate_pool)

        chunk_lookup: dict[str, dict] = {}
        dense_ids: list[str] = []
        for hit in dense_hits:
            cid = hit["chunk_id"]
            chunk_lookup[cid] = hit
            dense_ids.append(cid)

        bm25_ids: list[str] = []
        for hit in bm25_hits:
            cid = hit["chunk_id"]
            if cid not in chunk_lookup:
                chunk_lookup[cid] = hit
            bm25_ids.append(cid)

        fused = reciprocal_rank_fusion([dense_ids, bm25_ids], k=k_val)
        results: list[dict] = []
        for cid, rrf_score in fused[:limit]:
            doc = dict(chunk_lookup[cid])
            doc["rrf_score"] = rrf_score
            doc["matched_dense"] = cid in dense_ids
            doc["matched_bm25"] = cid in bm25_ids
            results.append(doc)
        return results


def ingest_vault_to_lancedb(
    settings: ISASettings | None = None,
    embedder: EmbedderProtocol | None = None,
    vector_store: LocalVectorStore | None = None,
) -> int:
    """Scan the Obsidian vault, split Markdown by headers, embed locally, and store in LanceDB."""
    cfg = settings or get_settings()
    store = vector_store or LocalVectorStore(cfg)
    emb = embedder or LocalEmbedder(cfg)
    splitter = MarkdownHeaderTextSplitter()

    md_files = discover_vault_markdown_files(
        cfg.obsidian_vault_path,
        exclude_filenames={cfg.inbox_filename},
    )
    all_chunks = []
    for file_path in md_files:
        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Skipping unreadable file %s: %s", file_path, exc)
            continue
        rel_path = str(file_path.relative_to(cfg.obsidian_vault_path))
        chunks = splitter.split_text(text, file_path=rel_path)
        all_chunks.extend(chunks)

    if not all_chunks:
        logger.info("No Markdown chunks found to ingest from %s.", cfg.obsidian_vault_path)
        return 0

    texts_to_embed = [
        f"File: {c.file_path}\nSection: {c.heading_hierarchy}\n\n{c.text}"
        for c in all_chunks
    ]
    vectors = emb.embed_documents(texts_to_embed)

    records = [
        VaultChunkRecord(
            chunk_id=chunk.chunk_id,
            file_path=chunk.file_path,
            heading_hierarchy=chunk.heading_hierarchy,
            text=chunk.text,
            vector=vec,
        )
        for chunk, vec in zip(all_chunks, vectors, strict=True)
    ]
    count = store.upsert_chunks(records)
    logger.info(
        "Ingested %d Markdown chunks across %d files into LanceDB (%s).",
        count,
        len(md_files),
        store.uri,
    )
    return count
