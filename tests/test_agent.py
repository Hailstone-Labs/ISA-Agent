"""Unit tests for air-gap settings, PydanticAI agent, Markdown chunking, and LanceDB Hybrid RRF."""

from __future__ import annotations

import os
import pytest
from config.settings import ISASettings, enforce_airgap_env
from src.theme import ensure_obsidian_themes_snippet, get_theme
from src.agent import (
    ISADependencies,
    build_isa_agent,
    build_local_model,
    dispatch_inbox_command,
    format_inbox_response,
    format_inline_note_response,
    format_thinking_block,
)
from src.memory.context_window import RollingContextWindow, parse_inbox_entries
from src.memory.ingest import MarkdownHeaderTextSplitter
from src.memory.vector_db import (
    LocalVectorStore,
    extract_wikilinks,
    ingest_vault_to_lancedb,
    reciprocal_rank_fusion,
    sync_single_vault_file_to_lancedb,
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


def test_incremental_single_file_lancedb_sync(tmp_path) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    note_a = vault_dir / "NoteA.md"
    note_a.write_text("# Note A\nMarketing strategy overview.\n", encoding="utf-8")

    settings = ISASettings(
        OBSIDIAN_VAULT_PATH=vault_dir,
        LANCEDB_URI=tmp_path / "lancedb",
    )
    embedder = DummyEmbedder()
    store = LocalVectorStore(settings)

    ingest_vault_to_lancedb(settings=settings, embedder=embedder, vector_store=store)
    assert len(store.get_all_chunks()) == 1

    # Add NoteB incrementally without re-ingesting NoteA
    note_b = vault_dir / "NoteB.md"
    note_b.write_text("# Note B\nSecret codename PROJECT-X99-OMEGA.\n", encoding="utf-8")
    upserted = sync_single_vault_file_to_lancedb(
        file_path=note_b,
        settings=settings,
        embedder=embedder,
        vector_store=store,
    )
    assert upserted == 1
    assert len(store.get_all_chunks()) == 2

    # Delete NoteB from disk and sync its removal
    note_b.unlink()
    sync_single_vault_file_to_lancedb(
        file_path=note_b,
        settings=settings,
        embedder=embedder,
        vector_store=store,
    )
    assert len(store.get_all_chunks()) == 1


def test_wikilink_one_hop_graph_traversal_in_hybrid_search(tmp_path) -> None:
    assert extract_wikilinks("See [[Architecture Spec#Overview|Spec]] and [[Budget 2026]].") == [
        "Architecture Spec",
        "Budget 2026",
    ]

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "Project_Apollo.md").write_text(
        "# Project Apollo\nUnique token ZETA-991-LAUNCH. Linked dependency: [[Vendor_Contract_Delta]].\n",
        encoding="utf-8",
    )
    (vault_dir / "Vendor_Contract_Delta.md").write_text(
        "# Vendor Contract Delta\nPenalty clause is $50,000 per week of delay.\n",
        encoding="utf-8",
    )

    settings = ISASettings(
        OBSIDIAN_VAULT_PATH=vault_dir,
        LANCEDB_URI=tmp_path / "lancedb",
    )
    embedder = DummyEmbedder()
    store = LocalVectorStore(settings)
    ingest_vault_to_lancedb(settings=settings, embedder=embedder, vector_store=store)

    hits = store.hybrid_search(
        query="ZETA-991-LAUNCH",
        embedder=embedder,
        limit=1,
        expand_wikilinks=True,
    )
    file_paths = [h["file_path"] for h in hits]
    assert "Project_Apollo.md" in file_paths
    assert "Vendor_Contract_Delta.md" in file_paths
    graph_hits = [h for h in hits if h.get("matched_graph")]
    assert len(graph_hits) == 1
    assert graph_hits[0]["file_path"] == "Vendor_Contract_Delta.md"


def test_theme_system_formatting_and_css_snippet(tmp_path) -> None:
    # 1. Theme resolution & aliases
    t_def = get_theme("default")
    assert t_def.name == "default"
    assert t_def.callout_response_tag == "NOTE"

    t_term = get_theme("matrix")
    assert t_term.name == "terminal"
    assert t_term.callout_response_tag == "isa-terminal"
    assert t_term.callout_thinking_tag == "isa-terminal-thinking"

    t_cyber = get_theme("2077")
    assert t_cyber.name == "cyberpunk"
    assert t_cyber.callout_response_tag == "isa-cyberpunk"
    assert t_cyber.callout_thinking_tag == "isa-cyberpunk-thinking"

    # 2. Status message translations
    assert "[LANCE_RRF]" in t_term.translate_status_message("Searching vault memory for 'notes'")
    assert "JACKING IN" in t_cyber.translate_status_message("Searching vault memory for 'notes'")

    # 3. Thinking block rendering
    thinking_term = format_thinking_block("Inspect servers", status_line="Checking status", theme_name="terminal")
    assert "> [!isa-terminal-thinking]" in thinking_term
    assert "ISA_KERNEL" in thinking_term
    assert "**EXEC_IN:**" in thinking_term

    thinking_cyber = format_thinking_block("Hack Arasaka", status_line="Scanning", theme_name="cyberpunk")
    assert "> [!isa-cyberpunk-thinking]" in thinking_cyber
    assert "RELIC HUD" in thinking_cyber
    assert "**TARGET_INTEL:**" in thinking_cyber

    # 4. Response callout formatting
    resp_term = format_inbox_response("Kernel panic: false", theme_name="terminal")
    assert "> [!isa-terminal]" in resp_term
    assert "[SYS_OK]" in resp_term

    resp_cyber = format_inbox_response("Gig completed, choom.", theme_name="cyberpunk")
    assert "> [!isa-cyberpunk]" in resp_cyber
    assert "NIGHT CITY INTEL FEED" in resp_cyber

    # 5. Inline response formatting
    inline_cyber = format_inline_note_response("Sync shard", "- [ ] Review eddies", theme_name="cyberpunk")
    assert "> [!isa-cyberpunk]" in inline_cyber
    assert "CHROME_FEED" in inline_cyber
    assert "- [ ]" not in inline_cyber
    assert "> - • Review eddies" in inline_cyber

    # 6. Obsidian CSS snippet installation & appearance.json registration
    css_path = ensure_obsidian_themes_snippet(tmp_path)
    assert css_path.exists()
    assert "isa-terminal" in css_path.read_text(encoding="utf-8")
    assert "isa-cyberpunk" in css_path.read_text(encoding="utf-8")
    app_json = tmp_path / ".obsidian" / "appearance.json"
    assert app_json.exists()
    assert "isa-themes" in app_json.read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_theme_slash_command_switching(tmp_path) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    inbox_file = vault_dir / "ISA_Inbox.md"
    inbox_file.write_text("# ISA Inbox\n\n@ISA /theme cyberpunk\n", encoding="utf-8")

    class DummyClient:
        def __init__(self, root):
            self.root = root

        async def get_note(self, path: str) -> str:
            p = self.root / path.lstrip("/")
            return p.read_text(encoding="utf-8") if p.exists() else ""

        async def create_or_overwrite_note(self, path: str, content: str) -> None:
            p = self.root / path.lstrip("/")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

        async def append_to_note(self, path: str, content: str) -> None:
            p = self.root / path.lstrip("/")
            existing = p.read_text(encoding="utf-8") if p.exists() else ""
            p.write_text(existing + content, encoding="utf-8")

        async def execute_command(self, cmd: str) -> None:
            pass

    settings = ISASettings(
        OBSIDIAN_VAULT_PATH=vault_dir,
        LANCEDB_URI=tmp_path / "lancedb",
        ISA_THEME="default",
    )
    deps = ISADependencies(
        settings=settings,
        obsidian=DummyClient(vault_dir),
    )

    reply = await dispatch_inbox_command(command="/theme cyberpunk", deps=deps)
    assert "NIGHT CITY NETRUNNER THEME ACTIVATED" in reply
    assert deps.settings.isa_theme == "cyberpunk"
    assert (tmp_path / "isa_theme.txt").read_text(encoding="utf-8") == "cyberpunk"
    written_inbox = await deps.obsidian.get_note("ISA_Inbox.md")
    assert "> [!isa-cyberpunk]" in written_inbox

    # Switch to terminal
    reply_term = await dispatch_inbox_command(command="/theme terminal", deps=deps)
    assert "CRT TERMINAL THEME ACTIVATED" in reply_term
    assert deps.settings.isa_theme == "terminal"
    assert (tmp_path / "isa_theme.txt").read_text(encoding="utf-8") == "terminal"


@pytest.mark.anyio
async def test_import_calendar_fast_command(tmp_path) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    inbox_file = vault_dir / "ISA_Inbox.md"
    inbox_file.write_text("# ISA Inbox\n\n@ISA /import-calendar\n", encoding="utf-8")

    import_dir = vault_dir / "Imports" / "Calendar"
    import_dir.mkdir(parents=True)

    ics_content = (
        "BEGIN:VCALENDAR\n"
        "VERSION:2.0\n"
        "BEGIN:VEVENT\n"
        "UID:FAST-CMD-UID-999\n"
        "DTSTART:20261029T150000\n"
        "DTEND:20261029T160000\n"
        "SUMMARY:Fast Path Sync\n"
        "DESCRIPTION:Discuss import architecture: https://teams.microsoft.com/l/meetup-join/fastpath123\n"
        "LOCATION:Microsoft Teams\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )
    (import_dir / "meeting.ics").write_text(ics_content, encoding="utf-8")

    class DummyClient:
        def __init__(self, root):
            self.root = root

        async def get_note(self, path: str) -> str:
            p = self.root / path.lstrip("/")
            return p.read_text(encoding="utf-8") if p.exists() else ""

        async def create_or_overwrite_note(self, path: str, content: str) -> None:
            p = self.root / path.lstrip("/")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

        async def append_to_note(self, path: str, content: str) -> None:
            p = self.root / path.lstrip("/")
            existing = p.read_text(encoding="utf-8") if p.exists() else ""
            p.write_text(existing + content, encoding="utf-8")

        async def delete_note(self, path: str) -> None:
            p = self.root / path.lstrip("/")
            if p.exists():
                p.unlink()

        async def execute_command(self, cmd: str) -> None:
            pass

    settings = ISASettings(
        OBSIDIAN_VAULT_PATH=vault_dir,
        LANCEDB_URI=tmp_path / "lancedb",
    )
    deps = ISADependencies(
        settings=settings,
        obsidian=DummyClient(vault_dir),
        embedder=DummyEmbedder(),
    )

    # First run: imports meeting.ics
    reply = await dispatch_inbox_command(command="/import-calendar", deps=deps)
    assert "Calendar Import Complete" in reply
    assert "**New Events Created:** 1" in reply

    created_notes = list((vault_dir / "Calendar").glob("*.md"))
    assert len(created_notes) == 1
    content = created_notes[0].read_text(encoding="utf-8")
    assert "Fast Path Sync" in content
    assert "https://teams.microsoft.com/l/meetup-join/fastpath123" in content

    # Second run: identical file is skipped
    reply2 = await dispatch_inbox_command(command="/import-calendar", deps=deps)
    assert "Calendar Import Complete" in reply2
    assert "**Unchanged (Skipped):** 1" in reply2

