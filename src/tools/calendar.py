"""Obsidian Full Calendar tool integration with strict YAML frontmatter validation."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Literal
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
