"""Unit tests for Obsidian REST client, Watchdog handler, Full Calendar YAML, and Tasks emojis."""

from __future__ import annotations

import pytest
from config.settings import ISASettings
from src.agent import format_inbox_response
from src.installer_tui import (
    resolve_or_generate_obsidian_api_key,
    scaffold_vault_files,
    write_env_file,
)
from src.monitor.observer import (
    RESPONSE_END_MARKER,
    create_inbox_observer,
    extract_pending_command,
)
from datetime import datetime
from src.tools.calendar import CalendarEventSpec
from src.tools.documents import (
    SynthesizedDocumentSpec,
    expand_templater_expressions,
    validate_markdown_syntax,
    validate_vault_path,
)
from src.tools.notifications import ActiveReminderMonitor
from src.tools.obsidian import ObsidianRestClient
from src.tools.tasks import TaskSpec


def test_obsidian_client_rejects_non_loopback() -> None:
    bad_settings = ISASettings(OBSIDIAN_API_URL="https://example.com:27124")
    with pytest.raises(ValueError, match="Air-gap violation"):
        ObsidianRestClient(bad_settings)


def test_obsidian_client_loopback_valid() -> None:
    settings = ISASettings(
        OBSIDIAN_API_URL="https://127.0.0.1:27124",
        OBSIDIAN_API_KEY="test-token",
    )
    client = ObsidianRestClient(settings)
    assert client.base_url == "https://127.0.0.1:27124"


def test_extract_pending_command_ignores_already_answered() -> None:
    doc = (
        "# ISA Inbox\n\n"
        "@ISA First question?\n"
        + format_inbox_response("First answer")
    )
    assert extract_pending_command(doc) is None

    # Mid-sentence auto-save with just a single EOF newline and no terminal punctuation should NOT trigger
    doc_mid_sentence = doc + "\n@ISA Good E\n"
    assert extract_pending_command(doc_mid_sentence, require_newline=True) is None

    # Complete sentence with terminal punctuation + newline SHOULD trigger in default mode
    doc_with_new = doc + "\n@ISA Second question about tomorrow?\n"
    assert extract_pending_command(doc_with_new) == "Second question about tomorrow?"

    # But in manual_trigger_only mode, terminal punctuation + single newline does NOT trigger!
    assert (
        extract_pending_command(
            doc_with_new, require_newline=True, manual_trigger_only=True
        )
        is None
    )

    # In manual mode, pressing Enter twice (blank line after command) DOES trigger
    doc_double_enter = doc + "\n@ISA Draft standup notes\n\n"
    assert (
        extract_pending_command(
            doc_double_enter, require_newline=True, manual_trigger_only=True
        )
        == "Draft standup notes"
    )


def test_watchdog_inbox_handler_debouncing(tmp_path) -> None:
    inbox_file = tmp_path / "ISA_Inbox.md"
    triggered: list[str] = []

    observer, handler = create_inbox_observer(
        inbox_path=inbox_file,
        on_user_command=lambda cmd: triggered.append(cmd),
        debounce_seconds=0.0,
    )
    assert inbox_file.exists()

    inbox_file.write_text("# ISA Inbox\n\n@ISA Hello World!\n", encoding="utf-8")

    class DummyEvent:
        is_directory = False
        src_path = str(inbox_file)

    handler.on_modified(DummyEvent())
    assert triggered == ["Hello World!"]

    handler.on_modified(DummyEvent())
    assert len(triggered) == 1

    inbox_file.write_text(
        "# ISA Inbox\n\n@ISA Hello World!\n" + format_inbox_response("Hello!"),
        encoding="utf-8",
    )
    handler.record_agent_write(dispatched_command="Hello World!")
    handler.on_modified(DummyEvent())
    assert len(triggered) == 1
    assert RESPONSE_END_MARKER in inbox_file.read_text(encoding="utf-8")


def test_calendar_event_spec_yaml_frontmatter() -> None:
    event = CalendarEventSpec(
        title="Performance Review with Alice",
        date="2026-10-22",
        start_time="10:00",
        end_time="11:00",
        agenda_or_notes="### Agenda\n- Goal review\n- Q3 Feedback",
    )
    expected = (
        "---\n"
        "title: Performance Review with Alice\n"
        "allDay: false\n"
        "startTime: 10:00\n"
        "endTime: 11:00\n"
        "date: 2026-10-22\n"
        "endDate: 2026-10-22\n"
        "type: single\n"
        "---\n"
        "### Agenda\n"
        "- Goal review\n"
        "- Q3 Feedback\n"
    )
    assert event.to_markdown() == expected
    assert event.safe_filename("Calendar") == "Calendar/2026-10-22 - Performance Review with Alice.md"


def test_calendar_event_rejects_invalid_date_and_time() -> None:
    with pytest.raises(ValueError, match="Invalid calendar date"):
        CalendarEventSpec(
            title="Bad Date Event",
            date="2026-02-30",
            start_time="10:00",
            end_time="11:00",
        )
    with pytest.raises(ValueError, match="Invalid 24-hour time"):
        CalendarEventSpec(
            title="12h Time Event",
            date="2026-10-22",
            start_time="10:00 AM",
            end_time="11:00 AM",
        )


def test_task_spec_obsidian_tasks_emoji_syntax() -> None:
    task = TaskSpec(
        description="Submit the weekly budget report",
        recurrence="every week on Friday",
        due_date="2026-10-23",
        priority="high",
    )
    assert (
        task.to_task_line()
        == "- [ ] Submit the weekly budget report 🔁 every week on Friday 📅 2026-10-23 ⏫"
    )


def test_task_spec_rejects_february_30_and_bad_recurrence() -> None:
    with pytest.raises(ValueError, match="Invalid calendar date"):
        TaskSpec(description="Corrupt task", due_date="2026-02-30")

    with pytest.raises(ValueError, match="Must start with 'every '"):
        TaskSpec(description="Bad recurrence", recurrence="weekly on Friday")

    # Bare weekday 'every Friday' should auto-normalize to 'every week on Friday'
    normalized = TaskSpec(description="Weekly report", recurrence="every Friday")
    assert normalized.recurrence == "every week on Friday"


def test_active_reminder_monitor_scans_due_tasks_and_upcoming_events(tmp_path) -> None:
    cal_dir = tmp_path / "Calendar"
    cal_dir.mkdir()
    (cal_dir / "2026-10-21 - Sync.md").write_text(
        "---\n"
        "title: Executive Sync\n"
        "allDay: false\n"
        "startTime: 16:45\n"
        "endTime: 17:15\n"
        "date: 2026-10-21\n"
        "endDate: 2026-10-21\n"
        "type: single\n"
        "---\n",
        encoding="utf-8",
    )
    (tmp_path / "Tasks.md").write_text(
        "# Tasks\n\n"
        "- [ ] Submit budget report 🔁 every week on Friday 📅 2026-10-21 ⏫\n"
        "- [x] Already finished 📅 2026-10-21\n",
        encoding="utf-8",
    )

    monitor = ActiveReminderMonitor(vault_path=tmp_path, lead_minutes=15)
    sim_now = datetime(2026, 10, 21, 16, 35)
    due_tasks = monitor.scan_due_tasks(now=sim_now)
    upcoming_events = monitor.scan_upcoming_events(now=sim_now)

    assert due_tasks == [("Submit budget report", "2026-10-21")]
    assert upcoming_events == [("Executive Sync", "2026-10-21", "16:45")]


def test_document_guardrails_and_templater_expansion() -> None:
    with pytest.raises(ValueError, match="Unsafe vault path"):
        validate_vault_path("../secret.md")

    with pytest.raises(ValueError, match="Cannot overwrite protected vault file"):
        validate_vault_path("ISA_Inbox.md")

    with pytest.raises(ValueError, match="Malformed YAML frontmatter"):
        validate_markdown_syntax("---\ntitle: Unclosed YAML\n# Heading")

    # Outer code fence stripping and valid YAML frontmatter
    spec = SynthesizedDocumentSpec(
        target_path="Reports/Q4 Summary",
        content="```markdown\n---\ntitle: Q4 Summary\n---\n# Summary\nAll good.\n```",
    )
    assert spec.target_path == "Reports/Q4 Summary.md"
    assert spec.content.startswith("---\ntitle: Q4 Summary\n---\n# Summary")

    # Templater variable expansion
    raw_tp = '# <% tp.file.title %> — <% tp.date.now("YYYY-MM-DD") %>'
    expanded = expand_templater_expressions(
        raw_tp,
        note_title="Daily Standup",
        now=datetime(2026, 10, 21, 9, 30),
    )
    assert expanded == "# Daily Standup — 2026-10-21"


def test_installer_scaffolding_and_api_key_generation(tmp_path) -> None:
    vault = tmp_path / "NewVault"
    vault.mkdir()

    scaffold_vault_files(vault)
    assert (vault / "Calendar").is_dir()
    assert (vault / "Templates" / "Daily Standup Template.md").exists()
    assert (vault / "Tasks.md").exists()
    assert (vault / "ISA_Inbox.md").exists()

    key1 = resolve_or_generate_obsidian_api_key(vault)
    assert len(key1) == 64
    # Calling again should preserve the existing key
    key2 = resolve_or_generate_obsidian_api_key(vault)
    assert key1 == key2

    env_path = write_env_file(project_root=tmp_path, vault_path=vault, api_key=key1)
    env_text = env_path.read_text(encoding="utf-8")
    assert f"OBSIDIAN_API_KEY={key1}" in env_text
    assert "OLLAMA_NO_CLOUD=1" in env_text


def test_task_deduplication_and_listing(tmp_path) -> None:
    from src.tools.tasks import _normalize_task_description

    norm1 = _normalize_task_description("- [ ] Submit the weekly budget report 🔁 every week on Friday 📅 2026-10-23 ⏫")
    norm2 = _normalize_task_description("Submit the weekly budget report")
    assert norm1 == norm2 == "submit the weekly budget report"


def test_calendar_listing_and_cancellation(tmp_path) -> None:
    from src.tools.calendar import list_vault_calendar_events

    cal_dir = tmp_path / "Calendar"
    cal_dir.mkdir()
    event_file = cal_dir / "2026-10-25 - Project Sync.md"
    event_file.write_text(
        "---\n"
        "title: Project Sync\n"
        "allDay: false\n"
        "startTime: 14:00\n"
        "endTime: 15:00\n"
        "date: 2026-10-25\n"
        "endDate: 2026-10-25\n"
        "type: single\n"
        "---\n"
        "Discussion notes.\n",
        encoding="utf-8",
    )

    events = list_vault_calendar_events(vault_path=tmp_path, folder="Calendar")
    assert len(events) == 1
    assert events[0]["title"] == "Project Sync"
    assert events[0]["date"] == "2026-10-25"
