"""Unit tests for Outlook and Microsoft Teams .ics calendar importing."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import pytest
from unittest.mock import AsyncMock

from src.monitor.observer import is_fast_command_str
from src.tools.calendar import _parse_calendar_frontmatter
from src.tools.calendar_import import (
    ParsedCalendarEvent,
    clean_html_description,
    extract_teams_join_url,
    import_ics_file_to_vault,
    parse_ical_datetime,
    parse_ics_content,
    scan_vault_events_by_uid,
    unescape_ical_text,
    unfold_ical_lines,
)


SAMPLE_ICS_SINGLE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Microsoft Corporation//Outlook 16.0 MIMEDIR//EN
BEGIN:VEVENT
UID:040000008200E00074C5B7101A82E0080000000010B0E5A7C57EDB010000000000000000
DTSTART;TZID=America/Chicago:20261026T100000
DTEND;TZID=America/Chicago:20261026T110000
SUMMARY:Project Architecture Review
DESCRIPTION:Reviewing the Q4 system architecture\\n\\nhttps://teams.microsoft.com/l/meetup-join/19%3ameeting_NzY1Zj...%40thread.v2/0?context=%7b%22Tid%22%3a%22...%22%7d\\n
LOCATION:Microsoft Teams Meeting
ORGANIZER;CN=Jane Doe:mailto:jane.doe@enterprise.com
ATTENDEE;CN=Bob Smith:mailto:bob.smith@enterprise.com
ATTENDEE;CN=Alice Johnson:mailto:alice.johnson@enterprise.com
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR"""


SAMPLE_ICS_ALL_DAY = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Microsoft Corporation//Outlook 16.0 MIMEDIR//EN
BEGIN:VEVENT
UID:ALLDAY-EVENT-UID-12345
DTSTART;VALUE=DATE:20261105
DTEND;VALUE=DATE:20261106
SUMMARY:Company Hackathon Day 1
DESCRIPTION:Annual engineering hackathon.
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR"""


SAMPLE_ICS_RECURRING = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Microsoft Corporation//Outlook 16.0 MIMEDIR//EN
BEGIN:VEVENT
UID:RECUR-WEEKLY-STANDUP-001
DTSTART:20261019T093000
DTEND:20261019T100000
RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR
SUMMARY:Engineering Morning Standup
LOCATION:Teams Call
DESCRIPTION:Daily sync: https://teams.microsoft.com/l/meetup-join/standup123
END:VEVENT
END:VCALENDAR"""


SAMPLE_ICS_HTML_DESC = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Microsoft Corporation//Outlook 16.0 MIMEDIR//EN
BEGIN:VEVENT
UID:HTML-DESC-EVENT-999
DTSTART:20261027T140000
DTEND:20261027T150000
SUMMARY:Vendor Sync
DESCRIPTION:<html><body><p>Agenda items:<br>&bull; Budget review<br>&bull; Contract scope</p><p>Join below:<br><a href="https://teams.microsoft.com/l/meetup-join/vendor789">Join Meeting</a></p></body></html>
LOCATION:Virtual
END:VEVENT
END:VCALENDAR"""


def test_unfold_ical_lines() -> None:
    raw = (
        "SUMMARY:This is a very long meeting title that Outlook fold\r\n"
        " ed onto two lines\r\n"
        "DESCRIPTION:Line 1\r\n"
        "\tcontinued line 1\r\n"
    )
    unfolded = unfold_ical_lines(raw)
    assert len(unfolded) == 2
    assert unfolded[0] == "SUMMARY:This is a very long meeting title that Outlook folded onto two lines"
    assert unfolded[1] == "DESCRIPTION:Line 1continued line 1"


def test_unescape_ical_text() -> None:
    raw = r"Project\, Review\; Status\nNew line\\escaped"
    unescaped = unescape_ical_text(raw)
    assert unescaped == "Project, Review; Status\nNew line\\escaped"


def test_clean_html_description() -> None:
    html = "<p>First paragraph<br>Second line</p><p>Another section &amp; details</p>"
    cleaned = clean_html_description(html)
    assert "First paragraph\nSecond line" in cleaned
    assert "Another section & details" in cleaned
    assert "<p>" not in cleaned
    assert "<br>" not in cleaned


def test_extract_teams_join_url() -> None:
    text = (
        "Please join on time.\n"
        "Meeting Link: https://teams.microsoft.com/l/meetup-join/19%3ameeting_ABC123%40thread.v2/0?context=%7b%22Tid%22%3a%22xyz%22%7d\n"
        "Dial in: +1 555 123 4567"
    )
    url = extract_teams_join_url(text)
    assert url == "https://teams.microsoft.com/l/meetup-join/19%3ameeting_ABC123%40thread.v2/0?context=%7b%22Tid%22%3a%22xyz%22%7d"

    # Also test teams.live.com meet links
    live_url = extract_teams_join_url("Join here: https://teams.live.com/meet/987654321")
    assert live_url == "https://teams.live.com/meet/987654321"

    assert extract_teams_join_url("No link in this description") is None


def test_parse_ical_datetime() -> None:
    # All-day DATE
    dt, is_all_day = parse_ical_datetime("20261026", {"VALUE": "DATE"})
    assert is_all_day is True
    assert dt.strftime("%Y-%m-%d") == "2026-10-26"

    # Floating datetime
    dt_float, is_all_day = parse_ical_datetime("20261026T143000")
    assert is_all_day is False
    assert dt_float.strftime("%Y-%m-%d %H:%M") == "2026-10-26 14:30"

    # UTC datetime ending in Z (converted to local system time)
    dt_utc, is_all_day = parse_ical_datetime("20261026T180000Z")
    assert is_all_day is False
    assert isinstance(dt_utc, datetime)


def test_parse_ics_content_single_event() -> None:
    events = parse_ics_content(SAMPLE_ICS_SINGLE)
    assert len(events) == 1
    ev = events[0]

    assert ev.title == "Project Architecture Review"
    assert ev.date == "2026-10-26"
    assert ev.end_date == "2026-10-26"
    assert ev.all_day is False
    assert ev.start_time is not None
    assert ev.end_time is not None
    assert ev.teams_join_url is not None
    assert "teams.microsoft.com/l/meetup-join" in ev.teams_join_url
    assert ev.organizer == "Jane Doe (jane.doe@enterprise.com)"
    assert len(ev.attendees) == 2
    assert "Bob Smith (bob.smith@enterprise.com)" in ev.attendees
    assert "Alice Johnson (alice.johnson@enterprise.com)" in ev.attendees
    assert ev.uid == "040000008200E00074C5B7101A82E0080000000010B0E5A7C57EDB010000000000000000"


def test_parse_ics_content_all_day_event() -> None:
    events = parse_ics_content(SAMPLE_ICS_ALL_DAY)
    assert len(events) == 1
    ev = events[0]

    assert ev.title == "Company Hackathon Day 1"
    assert ev.date == "2026-11-05"
    # RFC 5545 end date 20261106 exclusive -> 2026-11-05 inclusive
    assert ev.end_date == "2026-11-05"
    assert ev.all_day is True
    assert ev.start_time is None
    assert ev.end_time is None


def test_parse_ics_content_recurring_events() -> None:
    events = parse_ics_content(SAMPLE_ICS_RECURRING, horizon_days=30)
    # Recurring 3 days a week for 30 days should produce at least 10 instances
    assert len(events) >= 8
    for ev in events:
        assert ev.title == "Engineering Morning Standup"
        assert ev.all_day is False
        assert ev.start_time == "09:30"
        assert ev.end_time == "10:00"
        assert "RECUR-WEEKLY-STANDUP-001_" in ev.uid
        assert ev.teams_join_url == "https://teams.microsoft.com/l/meetup-join/standup123"


def test_parse_ics_content_html_cleaning() -> None:
    events = parse_ics_content(SAMPLE_ICS_HTML_DESC)
    assert len(events) == 1
    ev = events[0]
    assert ev.title == "Vendor Sync"
    assert "Budget review" in ev.description
    assert "Contract scope" in ev.description
    assert "<p>" not in ev.description
    assert ev.teams_join_url == "https://teams.microsoft.com/l/meetup-join/vendor789"


def test_parsed_event_to_markdown() -> None:
    ev = ParsedCalendarEvent(
        uid="TEST-UID-ABC-123",
        title="Q4 Strategy Review",
        date="2026-10-28",
        end_date="2026-10-28",
        all_day=False,
        start_time="13:00",
        end_time="14:00",
        location="Boardroom B",
        teams_join_url="https://teams.microsoft.com/l/meetup-join/q4strat",
        organizer="Jane Smith (jane@example.com)",
        attendees=["Mark Director (mark@example.com)"],
        description="Review executive deck before Friday.",
    )
    md = ev.to_markdown()
    meta = _parse_calendar_frontmatter(md)

    assert meta["title"] == "Q4 Strategy Review"
    assert str(meta["date"]) == "2026-10-28"
    assert str(meta["endDate"]) == "2026-10-28"
    assert meta["allDay"] is False
    assert "startTime: 13:00" in md
    assert "endTime: 14:00" in md
    assert meta["uid"] == "TEST-UID-ABC-123"
    assert meta["source"] == "outlook"

    assert "[🔗 Join Microsoft Teams Meeting](https://teams.microsoft.com/l/meetup-join/q4strat)" in md
    assert "**Organizer:** Jane Smith (jane@example.com)" in md
    assert "**Attendees:** Mark Director (mark@example.com)" in md
    assert "Review executive deck before Friday." in md


@pytest.mark.anyio
async def test_import_ics_file_to_vault_lifecycle(tmp_path: Path) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True)
    cal_dir = vault_dir / "Calendar"
    cal_dir.mkdir()
    ics_file = tmp_path / "test_meetings.ics"
    ics_file.write_text(SAMPLE_ICS_SINGLE, encoding="utf-8")

    written_notes: dict[str, str] = {}
    deleted_notes: list[str] = []

    mock_obsidian = AsyncMock()

    async def mock_create(path: str, content: str) -> None:
        written_notes[path] = content
        full_p = vault_dir / path
        full_p.parent.mkdir(parents=True, exist_ok=True)
        full_p.write_text(content, encoding="utf-8")

    async def mock_delete(path: str) -> None:
        deleted_notes.append(path)
        full_p = vault_dir / path
        if full_p.exists():
            full_p.unlink()

    mock_obsidian.create_or_overwrite_note.side_effect = mock_create
    mock_obsidian.delete_note.side_effect = mock_delete

    # 1. First import: event should be created
    res1 = await import_ics_file_to_vault(
        obsidian=mock_obsidian,
        vault_root=vault_dir,
        ics_path=ics_file,
        calendar_folder="Calendar",
        notify=False,
    )
    assert res1.created == 1
    assert res1.updated == 0
    assert res1.skipped == 0
    assert len(written_notes) == 1

    note_path = list(written_notes.keys())[0]
    assert note_path.startswith("Calendar/2026-10-26 - Project Architecture Review.md")

    # 2. Re-import identical file: should be skipped
    res2 = await import_ics_file_to_vault(
        obsidian=mock_obsidian,
        vault_root=vault_dir,
        ics_path=ics_file,
        calendar_folder="Calendar",
        notify=False,
    )
    assert res2.created == 0
    assert res2.updated == 0
    assert res2.skipped == 1

    # 3. Import updated event (e.g. rescheduled to 2026-10-27)
    rescheduled_ics = SAMPLE_ICS_SINGLE.replace("20261026T100000", "20261027T140000").replace("20261026T110000", "20261027T150000")
    ics_file.write_text(rescheduled_ics, encoding="utf-8")

    res3 = await import_ics_file_to_vault(
        obsidian=mock_obsidian,
        vault_root=vault_dir,
        ics_path=ics_file,
        calendar_folder="Calendar",
        notify=False,
    )
    assert res3.created == 0
    assert res3.updated == 1
    assert res3.skipped == 0
    assert len(deleted_notes) == 1
    assert deleted_notes[0] == note_path
    assert any("2026-10-27" in k for k in written_notes.keys())


def test_fast_command_recognition() -> None:
    assert is_fast_command_str("/import-calendar") is True
    assert is_fast_command_str("import-calendar") is True
    assert is_fast_command_str("/import-calendar Imports/Calendar/meetings.ics") is True
    assert is_fast_command_str("/import-ics") is True
    assert is_fast_command_str("import-ics file.ics") is True
    assert is_fast_command_str("/rescan") is True
    assert is_fast_command_str("/theme terminal") is True
    assert is_fast_command_str("Schedule a meeting tomorrow") is False
