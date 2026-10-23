"""Unit tests for air-gap settings, PydanticAI agent, Markdown chunking, and LanceDB Hybrid RRF."""

from __future__ import annotations

import os
import pytest
from config.settings import ISASettings, enforce_airgap_env
from src.agent import build_isa_agent, build_local_model, format_inbox_response
from src.memory.context_window import RollingContextWindow, parse_inbox_entries
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


def test_rolling_context_window_retains_last_ten_entries(tmp_path) -> None:
    persist_file = tmp_path / "rolling_context.json"
    window = RollingContextWindow(max_entries=10, persist_path=persist_file)

    for i in range(1, 16):
        window.add_entry(
            user_prompt=f"Question {i}",
            assistant_response=f"Answer {i}",
            timestamp=f"2026-10-22 10:{i:02d}:00",
        )

    assert len(window) == 10
    assert window.entries[0].user_prompt == "Question 6"
    assert window.entries[-1].user_prompt == "Question 15"

    # Reload from disk and verify persistence of the 10-entry window
    reloaded = RollingContextWindow(max_entries=10, persist_path=persist_file)
    assert len(reloaded) == 10
    assert reloaded.entries[0].user_prompt == "Question 6"
    assert reloaded.entries[-1].assistant_response == "Answer 15"

    prompt_block = reloaded.format_for_system_prompt()
    assert "Last 10 of max 10 entries" in prompt_block
    assert "Question 6" in prompt_block
    assert "Question 5" not in prompt_block

    # Test clearing memory resets both in-memory entries and persisted JSON file
    reloaded.clear()
    assert len(reloaded) == 0
    assert persist_file.read_text(encoding="utf-8").strip() == "[]"

    # Test self-healing recovery when rolling_context.json is corrupted
    persist_file.write_text("{corrupted_json: [1, 2", encoding="utf-8")
    recovered = RollingContextWindow(max_entries=10, persist_path=persist_file)
    assert len(recovered) == 0
    assert persist_file.read_text(encoding="utf-8").strip() == "[]"


def test_rolling_context_window_pydantic_messages_and_tool_evidence(tmp_path) -> None:
    persist_file = tmp_path / "rolling_context.json"
    window = RollingContextWindow(max_entries=5, persist_path=persist_file)
    window.add_entry(
        user_prompt="What meetings do I have tomorrow?",
        assistant_response="You have a sync with Alice at 10:00.",
        tool_context=["Found Calendar/2026-10-24 - Sync with Alice.md"],
    )
    assert len(window) == 1
    assert window.entries[0].tool_context == ["Found Calendar/2026-10-24 - Sync with Alice.md"]

    messages = window.to_pydantic_messages()
    assert len(messages) == 2
    assert "What meetings do I have tomorrow?" in messages[0].parts[0].content
    assert "Found Calendar/2026-10-24 - Sync with Alice.md" in messages[1].parts[0].content


def test_format_inbox_response_has_clean_callout_spacing_and_cursor() -> None:
    formatted = format_inbox_response("Here is the answer.", include_next_prompt_stub=True)
    # Must have blank line between callout and marker
    assert "> Here is the answer.\n\n<!-- ISA_RESPONSE_END -->" in formatted
    # Must have horizontal rule and Templater cursor placeholder well below the callout
    assert "---\n\n@ISA <% tp.file.cursor() %>\n" in formatted


def test_sync_rolling_window_from_inbox_markdown() -> None:
    parts = ["# ISA Inbox\n"]
    for i in range(1, 13):
        parts.append(f"\n@ISA Prompt number {i}\n")
        parts.append(format_inbox_response(f"Response number {i}"))
    # Add a trailing unanswered prompt (should not be included in completed history)
    parts.append("\n@ISA Pending question 13\n")
    inbox_md = "".join(parts)

    parsed = parse_inbox_entries(inbox_md)
    assert len(parsed) == 12

    window = RollingContextWindow(max_entries=10)
    count = window.sync_from_inbox_markdown(inbox_md)
    assert count == 10
    assert window.entries[0].user_prompt == "Prompt number 3"
    assert window.entries[-1].user_prompt == "Prompt number 12"
