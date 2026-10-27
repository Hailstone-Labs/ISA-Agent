"""Obsidian Full Calendar tool integration with strict YAML frontmatter validation."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import re
from typing import Literal
import dateutil.parser
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from src.tools.notifications import send_macos_notification
from src.tools.obsidian import ObsidianRestClient

TIME_24H_REGEX = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")
UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]+')

WEEKDAY_MAP: dict[str, int] = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}


def validate_iso_date(value: str) -> str:
    """Validate that a date string is a real calendar date in YYYY-MM-DD format."""
    cleaned = value.strip()
    try:
        parsed = datetime.strptime(cleaned, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(
            f"Invalid calendar date '{value}'. Expected valid YYYY-MM-DD date."
        ) from exc
    return parsed.strftime("%Y-%m-%d")


def resolve_date_string(value: str, base_dt: datetime | None = None) -> str:
    """Deterministically resolve an ISO date OR natural language date into YYYY-MM-DD.

    Supports:
    - Exact ISO dates: '2026-10-28' (validates real calendar dates; rejects '2026-02-30')
    - Relative days: 'today', 'tonight', 'tomorrow', 'tmrw', 'day after tomorrow', 'yesterday'
    - Relative offsets: 'in 3 days', '3 days from now', 'in 2 weeks', 'next week'
    - Weekdays: 'Monday', 'this Friday', 'next Tuesday', 'on Wednesday'
    - Named holidays / landmarks: 'Halloween', 'day after Halloween', 'Christmas', 'New Year's Eve'
    - Natural month/day strings: 'Oct 30', 'October 30th', 'Nov 1, 2026', '10/30/2026'
    """
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Date string cannot be empty.")

    # 1. If formatted like YYYY-MM-DD, strictly validate as an ISO calendar date
    if ISO_DATE_PATTERN.match(cleaned):
        return validate_iso_date(cleaned)

    now = base_dt or datetime.now()
    today = now.date()
    low = re.sub(r"\s+", " ", cleaned.lower()).strip(" .,")

    # Strip leading prepositions like "on " or "for "
    for prefix in ("on ", "for ", "by "):
        if low.startswith(prefix):
            low = low[len(prefix) :].strip()

    # 2. Direct relative keywords
    if low in {"today", "tonight", "this evening", "this afternoon", "this morning"}:
        return today.strftime("%Y-%m-%d")
    if low in {"tomorrow", "tmrw", "tmw"}:
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    if low in {"day after tomorrow", "the day after tomorrow"}:
        return (today + timedelta(days=2)).strftime("%Y-%m-%d")
    if low == "yesterday":
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")
    if low == "next week":
        return (today + timedelta(days=7)).strftime("%Y-%m-%d")

    # 3. Relative offsets: "in N days", "N days from now", "in N weeks"
    offset_match = re.match(
        r"^(?:in\s+)?(\d+)\s+(day|days|week|weeks)(?:\s+from\s+(?:now|today))?$", low
    )
    if offset_match:
        count = int(offset_match.group(1))
        unit = offset_match.group(2)
        days = count * 7 if unit.startswith("week") else count
        return (today + timedelta(days=days)).strftime("%Y-%m-%d")

    # 4. Weekday references: "monday", "this friday", "next tuesday"
    weekday_match = re.match(
        r"^(?:(this|next|coming|upcoming)\s+)?([a-z]+)$", low
    )
    if weekday_match and weekday_match.group(2) in WEEKDAY_MAP:
        modifier = weekday_match.group(1) or ""
        target_wd = WEEKDAY_MAP[weekday_match.group(2)]
        days_ahead = (target_wd - today.weekday()) % 7
        if days_ahead == 0:
            # If user says "next Tuesday" on a Tuesday, advance 7 days; if "this Tuesday" or "Tuesday", keep today or next
            days_ahead = 7 if modifier == "next" else 0
        elif modifier == "next" and days_ahead < 7:
            # Standard convention: "next <Weekday>" resolves to the upcoming occurrence
            pass
        return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    # 5. Common holiday landmarks
    year = today.year
    if "halloween" in low:
        halloween = date(year, 10, 31)
        if halloween < today - timedelta(days=30):
            halloween = date(year + 1, 10, 31)
        if "day after" in low:
            return (halloween + timedelta(days=1)).strftime("%Y-%m-%d")
        if "day before" in low or "eve" in low:
            return (halloween - timedelta(days=1)).strftime("%Y-%m-%d")
        return halloween.strftime("%Y-%m-%d")
    if low in {"christmas", "christmas day"}:
        xmas = date(year, 12, 25)
        if xmas < today:
            xmas = date(year + 1, 12, 25)
        return xmas.strftime("%Y-%m-%d")
    if low == "christmas eve":
        eve = date(year, 12, 24)
        if eve < today:
            eve = date(year + 1, 12, 24)
        return eve.strftime("%Y-%m-%d")

    # 6. Fallback to dateutil.parser for natural dates like "Oct 30", "November 1, 2026", "10/30/2026"
    try:
        default_dt = datetime(today.year, today.month, today.day)
        parsed = dateutil.parser.parse(cleaned, default=default_dt, fuzzy=False)
        return parsed.strftime("%Y-%m-%d")
    except (ValueError, OverflowError) as exc:
        raise ValueError(
            f"Invalid calendar date '{value}'. Expected YYYY-MM-DD or natural date (e.g. 'tomorrow', 'next Tuesday', 'Oct 30')."
        ) from exc


def resolve_time_string(value: str) -> str:
    """Deterministically normalize a 12-hour or 24-hour time string into 24-hour HH:MM."""
    cleaned = value.strip()
    if TIME_24H_REGEX.match(cleaned):
        return cleaned

    low = cleaned.lower().replace(".", "").strip()
    if low == "noon":
        return "12:00"
    if low == "midnight":
        return "00:00"

    # Match 12-hour or single-digit 24-hour patterns: "9am", "9:30 AM", "2:15pm", "9:00"
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", low)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        meridiem = m.group(3)
        if minute < 0 or minute > 59:
            raise ValueError(f"Invalid minute in time '{value}'.")
        if meridiem:
            if hour < 1 or hour > 12:
                raise ValueError(f"Invalid 12-hour time '{value}'.")
            if meridiem == "pm" and hour != 12:
                hour += 12
            elif meridiem == "am" and hour == 12:
                hour = 0
        else:
            if hour < 0 or hour > 23:
                raise ValueError(f"Invalid 24-hour time '{value}'.")
        return f"{hour:02d}:{minute:02d}"

    raise ValueError(
        f"Invalid 24-hour time '{value}'. Expected HH:MM (e.g. '09:30' or '14:00')."
    )


def add_one_hour(hh_mm: str) -> str:
    """Return HH:MM advanced by 1 hour (capped at 23:59 within the same day)."""
    hour, minute = (int(x) for x in hh_mm.split(":"))
    if hour >= 23:
        return "23:59"
    return f"{hour + 1:02d}:{minute:02d}"


class CalendarEventSpec(BaseModel):
    """Strict Pydantic schema for an Obsidian Full Calendar event note."""

    title: str = Field(..., min_length=1, description="Event title")
    date: str = Field(..., description="Event start date in YYYY-MM-DD format")
    end_date: str | None = Field(
        default=None, description="Event end date in YYYY-MM-DD format"
    )
    all_day: bool = Field(default=False, description="Whether this is an all-day event")
    start_time: str | None = Field(
        default=None, description="Start time in 24-hour HH:MM format (e.g. 10:00)"
    )
    end_time: str | None = Field(
        default=None, description="End time in 24-hour HH:MM format (e.g. 11:00)"
    )
    event_type: Literal["single", "recurring"] = Field(
        default="single", description="Full Calendar event type"
    )
    agenda_or_notes: str = Field(
        default="", description="Optional Markdown body / agenda below YAML frontmatter"
    )
    uid: str | None = Field(
        default=None, description="Unique identifier for calendar sync and deduplication"
    )
    source: str | None = Field(
        default=None, description="Origin source for imported calendar events (e.g. 'outlook')"
    )

    @field_validator("date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        return validate_iso_date(v)

    @field_validator("end_date")
    @classmethod
    def _check_end_date(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        return validate_iso_date(v)

    @field_validator("start_time", "end_time")
    @classmethod
    def _check_time(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        cleaned = v.strip()
        if not TIME_24H_REGEX.match(cleaned):
            raise ValueError(
                f"Invalid 24-hour time '{v}'. Expected HH:MM (e.g. '09:30' or '14:00')."
            )
        return cleaned

    @model_validator(mode="after")
    def _populate_defaults_and_validate_times(self) -> CalendarEventSpec:
        if self.end_date is None:
            self.end_date = self.date
        if not self.all_day:
            if not self.start_time or not self.end_time:
                raise ValueError(
                    "Non-all-day events require both 'start_time' and 'end_time' in HH:MM format."
                )
        return self

    def safe_filename(self, folder: str = "Calendar") -> str:
        """Return the vault-relative path for the calendar event Markdown file."""
        clean_title = UNSAFE_FILENAME_CHARS.sub("-", self.title).strip()
        clean_folder = folder.strip("/")
        if clean_folder:
            return f"{clean_folder}/{self.date} - {clean_title}.md"
        return f"{self.date} - {clean_title}.md"

    def to_markdown(self) -> str:
        """Render the exact YAML frontmatter and Markdown body required by Obsidian Full Calendar."""
        all_day_str = "true" if self.all_day else "false"
        lines = [
            "---",
            f"title: {self.title}",
            f"allDay: {all_day_str}",
        ]
        if not self.all_day and self.start_time and self.end_time:
            lines.append(f"startTime: {self.start_time}")
            lines.append(f"endTime: {self.end_time}")
        lines.extend(
            [
                f"date: {self.date}",
                f"endDate: {self.end_date or self.date}",
                f"type: {self.event_type}",
            ]
        )
        if self.uid:
            lines.append(f"uid: {self.uid}")
        if self.source:
            lines.append(f"source: {self.source}")
        lines.append("---")
        body = self.agenda_or_notes.strip()
        if body:
            lines.append(body)
        return "\n".join(lines) + "\n"


async def create_vault_calendar_event(
    obsidian: ObsidianRestClient,
    event: CalendarEventSpec,
    folder: str = "Calendar",
    notify: bool = True,
    vault_path: Path | None = None,
) -> str:
    """Create a new Obsidian Full Calendar event note and refresh Full Calendar cache."""
    rel_path = event.safe_filename(folder=folder)
    markdown_payload = event.to_markdown()
    try:
        await obsidian.create_or_overwrite_note(rel_path, markdown_payload)
    except Exception:
        if vault_path is not None:
            local_file = (vault_path / rel_path).resolve()
            local_file.parent.mkdir(parents=True, exist_ok=True)
            local_file.write_text(markdown_payload, encoding="utf-8")
        else:
            raise
    try:
        await obsidian.execute_command("obsidian-full-calendar:full-calendar-reset")
    except Exception:
        pass
    if notify:
        time_label = "All Day" if event.all_day else f"{event.start_time}–{event.end_time}"
        send_macos_notification(
            title="ISA Scheduled Event",
            subtitle=f"{event.date} ({time_label})",
            message=event.title,
        )
    return rel_path


def _parse_calendar_frontmatter(md_text: str) -> dict:
    """Parse YAML frontmatter from a Calendar/*.md file and normalize YAML 1.1 types."""
    if not md_text.startswith("---"):
        return {}
    parts = md_text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        loaded = yaml.safe_load(parts[1])
        if not isinstance(loaded, dict):
            return {}
        # Normalize YAML 1.1 sexagesimal integers (e.g. 13:00 -> 780) back into HH:MM strings
        for time_key in ("startTime", "endTime"):
            raw_val = loaded.get(time_key)
            if isinstance(raw_val, int):
                loaded[time_key] = f"{raw_val // 60:02d}:{raw_val % 60:02d}"
        # Normalize datetime.date objects into YYYY-MM-DD strings
        for date_key in ("date", "endDate"):
            raw_date = loaded.get(date_key)
            if isinstance(raw_date, (date, datetime)):
                loaded[date_key] = raw_date.strftime("%Y-%m-%d")
        return loaded
    except Exception:
        return {}


def _extract_calendar_body(md_text: str) -> str:
    """Extract the Markdown body below YAML frontmatter from a Calendar/*.md file."""
    if not md_text.startswith("---"):
        return md_text.strip()
    parts = md_text.split("---", 2)
    if len(parts) < 3:
        return ""
    return parts[2].strip()


def list_vault_calendar_events(
    vault_path: Path,
    folder: str = "Calendar",
    date_filter: str | None = None,
) -> list[dict]:
    """Scan Calendar/*.md in the vault and return structured event records."""
    cal_dir = vault_path / folder.strip("/")
    if not cal_dir.exists():
        return []

    resolved_filter: str | None = None
    if date_filter and date_filter.strip():
        try:
            resolved_filter = resolve_date_string(date_filter)
        except ValueError:
            resolved_filter = date_filter.strip()

    events: list[dict] = []
    for md_file in sorted(cal_dir.glob("*.md")):
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = _parse_calendar_frontmatter(text)
        ev_date = str(meta.get("date", "")).strip()
        if resolved_filter and ev_date != resolved_filter:
            continue
        rel_path = str(md_file.relative_to(vault_path))
        events.append(
            {
                "file_path": rel_path,
                "title": str(meta.get("title", md_file.stem)).strip(),
                "date": ev_date,
                "end_date": str(meta.get("endDate", ev_date)).strip(),
                "all_day": bool(meta.get("allDay", False)),
                "start_time": str(meta.get("startTime", "")).strip() or None,
                "end_time": str(meta.get("endTime", "")).strip() or None,
            }
        )
    return events


async def cancel_vault_calendar_event(
    obsidian: ObsidianRestClient,
    vault_path: Path,
    title_query: str,
    date_filter: str | None = None,
    folder: str = "Calendar",
    notify: bool = True,
) -> list[str]:
    """Delete matching calendar event note(s) and reset the Full Calendar plugin cache."""
    all_events = list_vault_calendar_events(
        vault_path=vault_path,
        folder=folder,
        date_filter=date_filter,
    )
    query_low = title_query.strip().lower()
    query_tokens = [tok for tok in re.findall(r"\w+", query_low) if len(tok) > 2]

    matched_events: list[dict] = []
    for ev in all_events:
        title_low = ev["title"].lower()
        file_low = ev["file_path"].lower()
        if (
            query_low in title_low
            or title_low in query_low
            or query_low in file_low
            or (query_tokens and all(tok in title_low for tok in query_tokens))
        ):
            matched_events.append(ev)

    deleted_paths: list[str] = []
    for ev in matched_events:
        rel_path = ev["file_path"]
        try:
            await obsidian.delete_note(rel_path)
            deleted_paths.append(rel_path)
        except Exception:
            # Fallback to local file unlink if REST API returns 404 or is unreachable in tests
            local_file = vault_path / rel_path
            if local_file.exists():
                local_file.unlink()
                deleted_paths.append(rel_path)

    if deleted_paths:
        try:
            await obsidian.execute_command("obsidian-full-calendar:full-calendar-reset")
        except Exception:
            pass
        if notify:
            send_macos_notification(
                title="ISA Canceled Event",
                subtitle=f"Removed {len(deleted_paths)} calendar entry",
                message=", ".join(Path(p).stem for p in deleted_paths),
            )
    return deleted_paths


async def reschedule_vault_calendar_event(
    obsidian: ObsidianRestClient,
    vault_path: Path,
    title_query: str,
    new_date: str | None = None,
    new_start_time: str | None = None,
    new_end_time: str | None = None,
    new_title: str | None = None,
    date_filter: str | None = None,
    folder: str = "Calendar",
    notify: bool = True,
) -> tuple[str, CalendarEventSpec] | None:
    """Reschedule or update an existing calendar event note, moving the file if date/title changed."""
    all_events = list_vault_calendar_events(
        vault_path=vault_path,
        folder=folder,
        date_filter=date_filter,
    )
    query_low = title_query.strip().lower()
    query_tokens = [tok for tok in re.findall(r"\w+", query_low) if len(tok) > 2]

    target_ev: dict | None = None
    for ev in all_events:
        title_low = ev["title"].lower()
        file_low = ev["file_path"].lower()
        if (
            query_low in title_low
            or title_low in query_low
            or query_low in file_low
            or (query_tokens and all(tok in title_low for tok in query_tokens))
        ):
            target_ev = ev
            break

    if target_ev is None:
        return None

    old_rel_path = target_ev["file_path"]
    old_file = vault_path / old_rel_path
    existing_body = ""
    if old_file.exists():
        try:
            existing_body = _extract_calendar_body(old_file.read_text(encoding="utf-8"))
        except OSError:
            pass

    updated_date = resolve_date_string(new_date) if new_date else target_ev["date"]
    updated_title = new_title.strip() if new_title else target_ev["title"]
    updated_start = (
        resolve_time_string(new_start_time)
        if new_start_time
        else target_ev["start_time"]
    )
    updated_end = (
        resolve_time_string(new_end_time) if new_end_time else target_ev["end_time"]
    )
    if updated_start and not updated_end:
        updated_end = add_one_hour(updated_start)
    all_day = bool(target_ev["all_day"]) and not (new_start_time or new_end_time)

    spec = CalendarEventSpec(
        title=updated_title,
        date=updated_date,
        end_date=updated_date,
        all_day=all_day,
        start_time=updated_start,
        end_time=updated_end,
        agenda_or_notes=existing_body,
    )
    new_rel_path = await create_vault_calendar_event(
        obsidian=obsidian,
        event=spec,
        folder=folder,
        notify=False,
        vault_path=vault_path,
    )
    if old_rel_path != new_rel_path:
        try:
            await obsidian.delete_note(old_rel_path)
        except Exception:
            if old_file.exists():
                old_file.unlink()
        try:
            await obsidian.execute_command("obsidian-full-calendar:full-calendar-reset")
        except Exception:
            pass

    if notify:
        time_label = (
            "All Day" if spec.all_day else f"{spec.start_time}–{spec.end_time}"
        )
        send_macos_notification(
            title="ISA Rescheduled Event",
            subtitle=f"{spec.date} ({time_label})",
            message=spec.title,
        )
    return new_rel_path, spec
