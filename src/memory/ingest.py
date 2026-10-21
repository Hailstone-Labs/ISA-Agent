"""Markdown-aware chunking pipeline and local embedding generation for Obsidian vaults."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
import re
from typing import Protocol
from fastembed import TextEmbedding
from config.settings import ISASettings, get_settings

logger = logging.getLogger("isa.memory.ingest")

HEADING_REGEX = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
FENCE_REGEX = re.compile(r"^\s*(`{3,}|~{3,})")


@dataclass
class MarkdownChunk:
    """A context-preserving chunk split along Markdown heading boundaries."""

    chunk_id: str
    file_path: str
    heading_hierarchy: str
    text: str


class EmbedderProtocol(Protocol):
    """Protocol for local text embedding generators."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, query: str) -> list[float]: ...


class MarkdownHeaderTextSplitter:
    """Splits Markdown documents along H1-H4 boundaries while preserving parent heading hierarchy."""

    def __init__(self, max_chunk_chars: int = 2000) -> None:
        self.max_chunk_chars = max_chunk_chars

    def split_text(self, markdown_text: str, file_path: str) -> list[MarkdownChunk]:
        """Parse a Markdown document into hierarchical heading-scoped chunks."""
        lines = markdown_text.splitlines()
        hierarchy: dict[int, str] = {}
        current_lines: list[str] = []
        raw_sections: list[tuple[str, str]] = []
        in_code_fence = False

        def current_breadcrumb() -> str:
            if not hierarchy:
                return Path(file_path).stem
            ordered = [hierarchy[level] for level in sorted(hierarchy.keys())]
            return " > ".join(ordered)

        def flush_section() -> None:
            non_empty = [ln for ln in current_lines if ln.strip()]
            has_non_heading = any(not HEADING_REGEX.match(ln) for ln in non_empty)
            body = "\n".join(current_lines).strip()
            if body and has_non_heading:
                raw_sections.append((current_breadcrumb(), body))
            current_lines.clear()

        for line in lines:
            if FENCE_REGEX.match(line):
                in_code_fence = not in_code_fence
                current_lines.append(line)
                continue

            if not in_code_fence:
                match = HEADING_REGEX.match(line)
                if match:
                    flush_section()
                    level = len(match.group(1))
                    heading_title = match.group(2).strip()
                    # Clear deeper heading levels when a higher-level heading appears
                    for existing_level in list(hierarchy.keys()):
                        if existing_level >= level:
                            del hierarchy[existing_level]
                    hierarchy[level] = heading_title
                    current_lines.append(line)
                    continue

            current_lines.append(line)

        flush_section()

        chunks: list[MarkdownChunk] = []
        chunk_idx = 0
        for breadcrumb, section_text in raw_sections:
            for sub_text in self._split_oversized(section_text):
                raw_id = f"{file_path}::{breadcrumb}::{chunk_idx}"
                chunk_id = hashlib.md5(raw_id.encode("utf-8")).hexdigest()
                chunks.append(
                    MarkdownChunk(
                        chunk_id=chunk_id,
                        file_path=file_path,
                        heading_hierarchy=breadcrumb,
                        text=sub_text,
                    )
                )
                chunk_idx += 1

        return chunks

    def _split_oversized(self, text: str) -> list[str]:
        """Split a section along paragraph boundaries only if it exceeds max_chunk_chars."""
        if len(text) <= self.max_chunk_chars:
            return [text]

        paragraphs = text.split("\n\n")
        sub_chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for para in paragraphs:
            para_len = len(para) + 2
            if current and (current_len + para_len) > self.max_chunk_chars:
                sub_chunks.append("\n\n".join(current).strip())
                current = [para]
                current_len = len(para)
            else:
                current.append(para)
                current_len += para_len

        if current:
            sub_chunks.append("\n\n".join(current).strip())

        return [c for c in sub_chunks if c]


class LocalEmbedder:
    """Local CPU/ANE embedding generator powered by FastEmbed (BAAI/bge-small-en-v1.5)."""

    def __init__(self, settings: ISASettings | None = None) -> None:
        self.settings = settings or get_settings()
        cache_dir = self.settings.lancedb_uri.parent / "models"
        cache_dir.mkdir(parents=True, exist_ok=True)
        has_cached_model = any(cache_dir.iterdir())
        self._model = TextEmbedding(
            model_name=self.settings.embedding_model,
            cache_dir=str(cache_dir),
            local_files_only=has_cached_model,
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Generate dense embedding vectors for a list of document chunks."""
        if not texts:
            return []
        embeddings = list(self._model.embed(texts))
        return [vec.tolist() for vec in embeddings]

    def embed_query(self, query: str) -> list[float]:
        """Generate a dense embedding vector for a search query."""
        embeddings = list(self._model.embed([query]))
        return embeddings[0].tolist()


def discover_vault_markdown_files(
    vault_path: Path,
    exclude_filenames: set[str] | None = None,
) -> list[Path]:
    """Recursively list all Markdown files in the vault excluding system folders and inbox."""
    excluded = exclude_filenames or set()
    md_files: list[Path] = []
    if not vault_path.exists():
        return md_files

    for path in sorted(vault_path.rglob("*.md")):
        rel_parts = path.relative_to(vault_path).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if path.name in excluded:
            continue
        md_files.append(path)
    return md_files
