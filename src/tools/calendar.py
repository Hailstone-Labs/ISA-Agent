"""Obsidian Full Calendar tool integration with strict YAML frontmatter validation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Literal
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from src.tools.notifications import send_macos_notification
from src.tools.obsidian import ObsidianRestClient

TIME_24H_REGEX = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]+')


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
                "---",
            ]
        )
        body = self.agenda_or_notes.strip()
        if body:
            lines.append(body)
        return "\n".join(lines) + "\n"


async def create_vault_calendar_event(
    obsidian: ObsidianRestClient,
    event: CalendarEventSpec,
    folder: str = "Calendar",
    notify: bool = True,
) -> str:
    """Create a new Obsidian Full Calendar event note and refresh Full Calendar cache."""
    vault_path = event.safe_filename(folder=folder)
    markdown_payload = event.to_markdown()
    await obsidian.create_or_overwrite_note(vault_path, markdown_payload)
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
    return vault_path


def _parse_calendar_frontmatter(md_text: str) -> dict:
    """Parse YAML frontmatter from a Calendar/*.md file."""
    if not md_text.startswith("---"):
        return {}
    parts = md_text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        loaded = yaml.safe_load(parts[1])
        return loaded if isinstance(loaded, dict) else {}
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

    events: list[dict] = []
    for md_file in sorted(cal_dir.glob("*.md")):
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = _parse_calendar_frontmatter(text)
        ev_date = str(meta.get("date", "")).strip()
        if date_filter and ev_date != date_filter.strip():
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

    updated_date = validate_iso_date(new_date) if new_date else target_ev["date"]
    updated_title = new_title.strip() if new_title else target_ev["title"]
    updated_start = (
        new_start_time.strip() if new_start_time else target_ev["start_time"]
    )
    updated_end = new_end_time.strip() if new_end_time else target_ev["end_time"]
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
