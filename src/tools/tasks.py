"""Obsidian Tasks plugin tool integration with inline emoji syntax and date validation."""

from __future__ import annotations

import re
from typing import Literal
from pydantic import BaseModel, Field, field_validator
from src.tools.calendar import validate_iso_date
from src.tools.notifications import send_macos_notification
from src.tools.obsidian import ObsidianRestClient

PRIORITY_EMOJI_MAP: dict[str, str] = {
    "high": "⏫",
    "medium": "🔼",
    "low": "🔽",
}
BARE_WEEKDAY_RE = re.compile(
    r"^every\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?$",
    re.IGNORECASE,
)


class TaskSpec(BaseModel):
    """Strict Pydantic schema for an Obsidian Tasks inline checklist item."""

    description: str = Field(..., min_length=1, description="Actionable task description")
    due_date: str | None = Field(
        default=None, description="Absolute due date in YYYY-MM-DD format (📅)"
    )
    scheduled_date: str | None = Field(
        default=None, description="Scheduled date in YYYY-MM-DD format (⏳)"
    )
    start_date: str | None = Field(
        default=None, description="Start date in YYYY-MM-DD format (🛫)"
    )
    recurrence: str | None = Field(
        default=None,
        description="Recurrence rule starting with 'every' (e.g. 'every week on Friday') (🔁)",
    )
    priority: Literal["high", "medium", "low", "normal"] = Field(
        default="normal",
        description="Task priority level (⏫ high, 🔼 medium, 🔽 low, or normal)",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Optional tags (e.g. ['#work', '#finance'])",
    )

    @field_validator("due_date", "scheduled_date", "start_date")
    @classmethod
    def _validate_optional_date(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        return validate_iso_date(v)

    @field_validator("recurrence")
    @classmethod
    def _validate_recurrence(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        cleaned = v.strip()
        if cleaned.startswith("🔁"):
            cleaned = cleaned.lstrip("🔁").strip()
        if not cleaned.lower().startswith("every "):
            raise ValueError(
                f"Invalid Obsidian Tasks recurrence rule '{v}'. Must start with 'every ' (e.g. 'every week on Friday')."
            )
        weekday_match = BARE_WEEKDAY_RE.match(cleaned)
        if weekday_match:
            day_name = weekday_match.group(1).capitalize()
            cleaned = f"every week on {day_name}"
        return cleaned

    def to_task_line(self) -> str:
        """Format the task into exact Obsidian Tasks inline emoji syntax."""
        parts: list[str] = ["- [ ]", self.description.strip()]

        for raw_tag in self.tags:
            tag = raw_tag.strip()
            if tag:
                parts.append(tag if tag.startswith("#") else f"#{tag}")

        if self.recurrence:
            parts.append(f"🔁 {self.recurrence}")
        if self.start_date:
            parts.append(f"🛫 {self.start_date}")
        if self.scheduled_date:
            parts.append(f"⏳ {self.scheduled_date}")
        if self.due_date:
            parts.append(f"📅 {self.due_date}")

        priority_emoji = PRIORITY_EMOJI_MAP.get(self.priority)
        if priority_emoji:
            parts.append(priority_emoji)

        return " ".join(parts)


async def append_vault_task(
    obsidian: ObsidianRestClient,
    task: TaskSpec,
    tasks_filename: str = "Tasks.md",
    notify: bool = True,
) -> str:
    """Append a formatted inline task to the designated active tasks note in Obsidian."""
    task_line = task.to_task_line()
    try:
        existing = await obsidian.get_note(tasks_filename)
        prefix = "" if existing.endswith("\n") else "\n"
        await obsidian.append_to_note(tasks_filename, f"{prefix}{task_line}\n")
    except Exception:
        # Note does not exist yet; create it with a clean header and live Tasks query block
        initial_doc = (
            "# Tasks\n\n"
            "## Active Dashboard\n"
            "```tasks\n"
            "not done\n"
            "sort by priority\n"
            "sort by due\n"
            "```\n\n"
            "## Task List\n\n"
            f"{task_line}\n"
        )
        await obsidian.create_or_overwrite_note(tasks_filename, initial_doc)

    if notify:
        due_label = f"Due {task.due_date}" if task.due_date else "Added to Tasks.md"
        send_macos_notification(
            title="ISA Reminder Created",
            subtitle=due_label,
            message=task.description,
        )
    return task_line
