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


class FakeObsidian:
    def __init__(self, root):
        self.root = root

    async def get_note(self, path: str) -> str:
        p = self.root / path.lstrip("/")
        if not p.exists():
            raise FileNotFoundError(path)
        return p.read_text(encoding="utf-8")

    async def create_or_overwrite_note(self, path: str, content: str) -> None:
        p = self.root / path.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    async def append_to_note(self, path: str, content: str) -> None:
        p = self.root / path.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        existing = p.read_text(encoding="utf-8") if p.exists() else ""
        p.write_text(existing + content, encoding="utf-8")

    async def delete_note(self, path: str) -> None:
        p = self.root / path.lstrip("/")
        if p.exists():
            p.unlink()

    async def execute_command(self, cmd: str) -> None:
        pass


@pytest.mark.anyio
async def test_complete_and_update_task_and_reschedule_event(tmp_path) -> None:
    from src.tools.calendar import reschedule_vault_calendar_event
    from src.tools.documents import (
        archive_inbox_transcript,
        clear_inbox_page,
        generate_daily_executive_briefing,
    )
    from src.tools.tasks import complete_vault_task, update_vault_task

    obs = FakeObsidian(tmp_path)
    await obs.create_or_overwrite_note(
        "Tasks.md",
        "# Tasks\n\n## Task List\n\n"
        "- [ ] Review the quarterly report 📅 2026-10-24 ⏫\n"
        "- [ ] Call the accountant 📅 2026-10-27 🔼\n",
    )

    # 1. Update task due date & priority
    updated = await update_vault_task(
        obsidian=obs,
        description_query="Call the accountant",
        new_due_date="2026-10-28",
        new_priority="high",
        notify=False,
    )
    assert updated == "- [ ] Call the accountant 📅 2026-10-28 ⏫"

    # 2. Complete task
    completed = await complete_vault_task(
        obsidian=obs,
        description_query="quarterly report",
        completion_date="2026-10-23",
        notify=False,
    )
    assert len(completed) == 1
    assert completed[0].startswith("- [x] Review the quarterly report 📅 2026-10-24 ⏫ ✅ 2026-10-23")

    # 3. Reschedule calendar event
    cal_dir = tmp_path / "Calendar"
    cal_dir.mkdir(exist_ok=True)
    (cal_dir / "2026-10-24 - Strategy Sync.md").write_text(
        "---\n"
        "title: Strategy Sync\n"
        "allDay: false\n"
        "startTime: 10:00\n"
        "endTime: 11:00\n"
        "date: 2026-10-24\n"
        "endDate: 2026-10-24\n"
        "type: single\n"
        "---\n"
        "### Notes\nBring Q4 deck.\n",
        encoding="utf-8",
    )
    res = await reschedule_vault_calendar_event(
        obsidian=obs,
        vault_path=tmp_path,
        title_query="Strategy Sync",
        new_date="2026-10-28",
        new_start_time="14:00",
        new_end_time="15:00",
        notify=False,
    )
    assert res is not None
    new_path, spec = res
    assert new_path == "Calendar/2026-10-28 - Strategy Sync.md"
    assert not (cal_dir / "2026-10-24 - Strategy Sync.md").exists()
    assert (cal_dir / "2026-10-28 - Strategy Sync.md").exists()
    assert spec.start_time == "14:00"
    assert "Bring Q4 deck." in spec.agenda_or_notes

    # 4. Generate Daily Executive Briefing
    brief_path, brief_md = await generate_daily_executive_briefing(
        obsidian=obs,
        vault_root=tmp_path,
        target_date="2026-10-28",
        notify=False,
    )
    assert brief_path == "Briefings/2026-10-28 - Executive Briefing.md"
    assert "Strategy Sync" in brief_md
    assert "Call the accountant" in brief_md

    # 5. Archive Inbox Transcript
    inbox_content = "# ISA Inbox\n"
    for i in range(1, 6):
        inbox_content += f"\n@ISA Question {i}?\n" + format_inbox_response(f"Answer {i}")
    await obs.create_or_overwrite_note("ISA_Inbox.md", inbox_content)

    arch_path, arch_count = await archive_inbox_transcript(
        obsidian=obs,
        inbox_filename="ISA_Inbox.md",
        keep_recent_exchanges=2,
        notify=False,
    )
    assert arch_count == 3
    assert arch_path is not None and arch_path.startswith("Archive/ISA_Transcript_")
    remaining_inbox = await obs.get_note("ISA_Inbox.md")
    assert "Question 4?" in remaining_inbox
    assert "Question 5?" in remaining_inbox
    assert "Question 1?" not in remaining_inbox

    # 6. Clear Inbox Page
    await clear_inbox_page(obsidian=obs, inbox_filename="ISA_Inbox.md", notify=False)
    cleared_inbox = await obs.get_note("ISA_Inbox.md")
    assert cleared_inbox.startswith("# ISA Inbox")
    assert "Question 4?" not in cleared_inbox
    assert "@ISA" in cleared_inbox


@pytest.mark.anyio
async def test_meeting_action_items_extraction_and_sync(tmp_path) -> None:
    from src.tools.documents import (
        parse_action_items_from_markdown,
        sync_meeting_action_items,
    )

    meetings_dir = tmp_path / "Meetings"
    meetings_dir.mkdir(parents=True)
    meeting_note = meetings_dir / "Q4 Product Sync.md"
    meeting_note.write_text(
        "# Q4 Product Sync\n\n"
        "## Discussion\n"
        "- ACTION: Send updated pricing sheet to Bob\n"
        "- [ ] Schedule security review with Alice\n"
        "- Follow-up: Finalize launch deck by Friday\n",
        encoding="utf-8",
    )

    parsed = parse_action_items_from_markdown(meeting_note.read_text(encoding="utf-8"))
    assert len(parsed) == 3
    assert "Send updated pricing sheet to Bob" in parsed
    assert "Schedule security review with Alice" in parsed
    assert "Finalize launch deck by Friday" in parsed

    obs = FakeObsidian(tmp_path)
    await obs.create_or_overwrite_note("Tasks.md", "# Active Tasks\n")

    rel_path, added_lines, skipped = await sync_meeting_action_items(
        obsidian=obs,
        vault_root=tmp_path,
        note_query="Q4 Product Sync",
        due_date="2026-10-30",
        tasks_filename="Tasks.md",
        notify=False,
    )
    assert rel_path == "Meetings/Q4 Product Sync.md"
    assert len(added_lines) == 3
    assert skipped == 0

    tasks_md = await obs.get_note("Tasks.md")
    assert "Send updated pricing sheet to Bob (from [[Q4 Product Sync]]) 📅 2026-10-30" in tasks_md
    assert "Schedule security review with Alice (from [[Q4 Product Sync]]) 📅 2026-10-30" in tasks_md

    # Verify the meeting note itself has no raw '- [ ]' checkboxes to prevent duplicate indexing
    updated_meeting = await obs.get_note("Meetings/Q4 Product Sync.md")
    assert "- [ ]" not in updated_meeting
    assert "## ⚡ Action Items" in updated_meeting
    assert "- • Send updated pricing sheet to Bob" in updated_meeting

    # Re-running sync should deduplicate all 3 items
    _, added_again, skipped_again = await sync_meeting_action_items(
        obsidian=obs,
        vault_root=tmp_path,
        note_query="Q4 Product Sync",
        due_date="2026-10-30",
        tasks_filename="Tasks.md",
        notify=False,
    )
    assert len(added_again) == 0
    assert skipped_again == 3


def test_inline_note_command_extraction_and_formatting() -> None:
    from src.agent import format_inline_note_response
    from src.monitor.observer import extract_inline_note_command

    note_md = (
        "# Project Roadmap\n\n"
        "Some context about Q4 milestones.\n\n"
        "@ISA Summarize the key risks for this milestone.\n\n"
        "## Next Section\n"
    )
    extracted = extract_inline_note_command(note_md, manual_trigger_only=True)
    assert extracted is not None
    cmd, start, end = extracted
    assert cmd == "Summarize the key risks for this milestone."

    callout = format_inline_note_response(
        command=cmd,
        response_text="- [ ] Check vendor timeline\n- All systems nominal.",
    )
    # Raw checkboxes inside inline callout must be converted to '- •'
    assert "- [ ]" not in callout
    assert "> - • Check vendor timeline" in callout

    replaced = note_md[:start] + callout + note_md[end:]
    # Once replaced, extract_inline_note_command must not re-trigger
    assert extract_inline_note_command(replaced, manual_trigger_only=True) is None


def test_scheduled_morning_briefing_trigger(tmp_path) -> None:
    monitor = ActiveReminderMonitor(vault_path=tmp_path)
    early_dt = datetime(2026, 10, 24, 7, 30)
    morning_dt = datetime(2026, 10, 24, 8, 5)

    # Before 08:00 -> False
    assert monitor.should_trigger_morning_briefing("08:00", now=early_dt) is False
    # At 08:05 -> True once
    assert monitor.should_trigger_morning_briefing("08:00", now=morning_dt) is True
    # Subsequent check on the same day -> False
    assert monitor.should_trigger_morning_briefing("08:00", now=morning_dt) is False


def test_deterministic_date_and_time_resolution() -> None:
    from src.tools.calendar import (
        _parse_calendar_frontmatter,
        add_one_hour,
        resolve_date_string,
        resolve_time_string,
    )

    # Anchor on Friday, October 23, 2026
    friday_anchor = datetime(2026, 10, 23, 15, 30)

    assert resolve_date_string("today", base_dt=friday_anchor) == "2026-10-23"
    assert resolve_date_string("tomorrow", base_dt=friday_anchor) == "2026-10-24"
    assert resolve_date_string("day after tomorrow", base_dt=friday_anchor) == "2026-10-25"
    assert resolve_date_string("yesterday", base_dt=friday_anchor) == "2026-10-22"
    assert resolve_date_string("in 3 days", base_dt=friday_anchor) == "2026-10-26"
    assert resolve_date_string("in 2 weeks", base_dt=friday_anchor) == "2026-11-06"

    # Weekday math from Friday Oct 23, 2026: Monday MUST be 2026-10-26 (not 2026-10-27!)
    assert resolve_date_string("Monday", base_dt=friday_anchor) == "2026-10-26"
    assert resolve_date_string("on Monday", base_dt=friday_anchor) == "2026-10-26"
    assert resolve_date_string("next Tuesday", base_dt=friday_anchor) == "2026-10-27"
    assert resolve_date_string("next Friday", base_dt=friday_anchor) == "2026-10-30"

    # Holiday & natural date landmarks
    assert resolve_date_string("Halloween", base_dt=friday_anchor) == "2026-10-31"
    assert resolve_date_string("the day after Halloween", base_dt=friday_anchor) == "2026-11-01"
    assert resolve_date_string("Oct 30", base_dt=friday_anchor) == "2026-10-30"

    # Strict validation still rejects impossible calendar dates
    with pytest.raises(ValueError, match="Invalid calendar date"):
        resolve_date_string("2026-02-30", base_dt=friday_anchor)

    # 12h & 24h time normalization
    assert resolve_time_string("9am") == "09:00"
    assert resolve_time_string("9:30 AM") == "09:30"
    assert resolve_time_string("2:15 pm") == "14:15"
    assert resolve_time_string("12:00 PM") == "12:00"
    assert resolve_time_string("12:00 AM") == "00:00"
    assert resolve_time_string("noon") == "12:00"
    assert resolve_time_string("14:30") == "14:30"
    assert add_one_hour("09:00") == "10:00"
    assert add_one_hour("14:30") == "15:30"

    # YAML 1.1 sexagesimal integer normalization in _parse_calendar_frontmatter
    sample_md = (
        "---\n"
        "title: Strategy Sync\n"
        "allDay: false\n"
        "startTime: 13:00\n"
        "endTime: 14:30\n"
        "date: 2026-10-28\n"
        "endDate: 2026-10-28\n"
        "type: single\n"
        "---\n"
    )
    meta = _parse_calendar_frontmatter(sample_md)
    assert meta["startTime"] == "13:00"
    assert meta["endTime"] == "14:30"
    assert meta["date"] == "2026-10-28"

