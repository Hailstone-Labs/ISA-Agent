"""Unit tests for air-gap settings, PydanticAI agent, Markdown chunking, and LanceDB Hybrid RRF."""

from __future__ import annotations

import os
import pytest
from config.settings import ISASettings, enforce_airgap_env
from src.agent import build_isa_agent, build_local_model
from src.memory.ingest import MarkdownHeaderTextSplitter
from src.memory.vector_db import (
    LocalVectorStore,
     ingest_vault_to_lancedb,
    reciprocal_rank_fusion,
)


class DummyEmbedder:
    """Deterministic lightweight embedder for fast unit testing."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(t) for t in texts]

    def embed_query(self, query: str) -> list[float]:
        q_low = query.lower()
        return [
            1.0 if "marketing" in q_low or "strategy" in q_low else 0.1,
            1.0 if "budget" in q_low or "finance" in q_low else 0.1,
            1.0 if "project-x99" in q_low else 0.1,
            0.5,
        ]


def test_enforce_airgap_env() -> None:
    enforced = enforce_airgap_env()
    assert enforced["OLLAMA_NO_CLOUD"] == "1"
    assert enforced["OLLAMA_NO_UPDATE_CHECK"] == "1"
    assert enforced["OLLAMA_HOST"] == "127.0.0.1:11434"
    assert os.environ["COPILOT_OFFLINE"] == "true"
    assert os.environ["OPENCODE_AIRGAPPED"] == "1"


def test_build_local_model_rejects_external_host() -> None:
    bad_settings = ISASettings(ISA_LLM_BASE_URL="https://api.openai.com/v1")
    with pytest.raises(ValueError, match="Air-gap violation"):
        build_local_model(bad_settings)


def test_build_isa_agent_loopback() -> None:
    settings = ISASettings(ISA_LLM_BASE_URL="http://127.0.0.1:11434/v1")
    agent = build_isa_agent(settings)
    assert agent is not None


def test_lancedb_initialization(tmp_path) -> None:
    settings = ISASettings(LANCEDB_URI=tmp_path / "lancedb")
    store = LocalVectorStore(settings)
    assert store.list_tables() == []


def test_reciprocal_rank_fusion() -> None:
    dense = ["doc_a", "doc_b", "doc_c"]
    bm25 = ["doc_b", "doc_d", "doc_a"]
    fused = reciprocal_rank_fusion([dense, bm25], k=60)
    assert fused[0][0] == "doc_b"
    assert fused[1][0] == "doc_a"


def test_markdown_header_splitter_preserves_hierarchy_and_code_fences() -> None:
    md = (
        "# Executive Summary\n"
        "High-level overview.\n\n"
        "## Q4 Marketing\n"
        "Focus on enterprise channels.\n"
        "```python\n"
        "# This comment inside a code fence is NOT a Markdown heading\n"
        "print('hello')\n"
        "```\n\n"
        "### Campaign Metrics\n"
        "Conversion rate reached 14%.\n"
    )
    splitter = MarkdownHeaderTextSplitter()
    chunks = splitter.split_text(md, file_path="Plans/Q4.md")
    assert len(chunks) == 3
    assert chunks[0].heading_hierarchy == "Executive Summary"
    assert chunks[1].heading_hierarchy == "Executive Summary > Q4 Marketing"
    assert "# This comment inside a code fence" in chunks[1].text
    assert (
        chunks[2].heading_hierarchy
        == "Executive Summary > Q4 Marketing > Campaign Metrics"
    )


def test_vault_ingest_and_hybrid_rrf_search(tmp_path) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "ISA_Inbox.md").write_text("# ISA Inbox\nShould be excluded.", encoding="utf-8")
    (vault_dir / "Strategy.md").write_text(
        "# Annual Plan\n\n## Marketing Strategy\nOur Q4 marketing strategy targets enterprise growth.\n\n"
        "## Engineering Codename\nThe secret initiative is codenamed PROJECT-X99-OMEGA.\n",
        encoding="utf-8",
    )

    settings = ISASettings(
        OBSIDIAN_VAULT_PATH=vault_dir,
        LANCEDB_URI=tmp_path / "lancedb",
    )
    embedder = DummyEmbedder()
    store = LocalVectorStore(settings)

    count = ingest_vault_to_lancedb(
        settings=settings,
        embedder=embedder,
        vector_store=store,
    )
    assert count == 2

    # Exact codename query should surface the Engineering Codename chunk via BM25 + RRF
    results = store.hybrid_search("PROJECT-X99-OMEGA", embedder=embedder, limit=2)
    assert len(results) >= 1
    assert "PROJECT-X99-OMEGA" in results[0]["text"]
    assert results[0]["matched_bm25"] is True
