"""Obsidian Tasks plugin tool integration with inline emoji syntax and date validation."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Literal
from pydantic import BaseModel, Field, field_validator
from src.tools.calendar import resolve_date_string, validate_iso_date
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


def _normalize_task_description(text: str) -> str:
    """Normalize a task line or description for duplicate detection."""
    cleaned = re.sub(r"^-\s*\[[ xX]\]\s*", "", text.strip())
    # Strip trailing Obsidian Tasks emojis and metadata
    cleaned = re.split(r"\s+(?:🔁|🛫|⏳|📅|⏫|🔼|🔽|#\w+)", cleaned)[0]
    return re.sub(r"\s+", " ", cleaned).strip().lower()


async def list_vault_tasks(
    obsidian: ObsidianRestClient,
    tasks_filename: str = "Tasks.md",
    include_completed: bool = False,
) -> list[str]:
    """Read Tasks.md and return current task checklist lines without modifying the file."""
    try:
        content = await obsidian.get_note(tasks_filename)
    except Exception:
        return []

    tasks: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("- [ ]"):
            tasks.append(stripped)
        elif include_completed and (
            stripped.startswith("- [x]") or stripped.startswith("- [X]")
        ):
            tasks.append(stripped)
    return tasks


async def append_vault_task(
    obsidian: ObsidianRestClient,
    task: TaskSpec,
    tasks_filename: str = "Tasks.md",
    notify: bool = True,
) -> tuple[str, bool]:
    """Append a formatted inline task to the designated active tasks note in Obsidian.

    Returns `(task_line, created_new)` where `created_new` is False if an open task
    with the same normalized description already exists in `Tasks.md`.
    """
    task_line = task.to_task_line()
    norm_new = _normalize_task_description(task.description)
    try:
        existing = await obsidian.get_note(tasks_filename)
        for line in existing.splitlines():
            stripped = line.strip()
            if stripped.startswith("- [ ]"):
                if _normalize_task_description(stripped) == norm_new:
                    return stripped, False
        prefix = "" if existing.endswith("\n") else "\n"
        await obsidian.append_to_note(tasks_filename, f"{prefix}{task_line}\n")
    except Exception:
        # Note does not exist yet; create it with a clean header and live Tasks query block
        # that aggregates vault tasks while filtering out ISA_Inbox.md and Templates/
        initial_doc = (
            "# Tasks\n\n"
            "## Active Dashboard\n"
            "```tasks\n"
            "not done\n"
            "path does not include ISA_Inbox\n"
            "path does not include Archive\n"
            "path does not include Briefings\n"
            "path does not include Reports\n"
            "path does not include Templates\n"
            "description regex does not match /^$/\n"
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
    return task_line, True


def _matches_task_query(task_line: str, query: str) -> bool:
    """Return True if an open task line matches a user's natural language query."""
    norm_line = _normalize_task_description(task_line)
    norm_q = _normalize_task_description(query)
    if not norm_q:
        return False
    if norm_q in norm_line or norm_line in norm_q:
        return True
    q_tokens = [tok for tok in re.findall(r"\w+", norm_q) if len(tok) > 2]
    return bool(q_tokens and all(tok in norm_line for tok in q_tokens))


async def complete_vault_task(
    obsidian: ObsidianRestClient,
    description_query: str,
    tasks_filename: str = "Tasks.md",
    completion_date: str | None = None,
    notify: bool = True,
) -> list[str]:
    """Mark matching open `- [ ]` task(s) in Tasks.md as `- [x] ... ✅ YYYY-MM-DD`."""
    try:
        content = await obsidian.get_note(tasks_filename)
    except Exception:
        return []

    done_date = completion_date or datetime.now().strftime("%Y-%m-%d")
    lines = content.splitlines()
    completed_lines: list[str] = []
    updated_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- [ ]") and _matches_task_query(
            stripped, description_query
        ):
            checked = re.sub(r"^(\s*-\s*)\[ \]", r"\1[x]", line, count=1).rstrip()
            if "✅" not in checked:
                checked = f"{checked} ✅ {done_date}"
            updated_lines.append(checked)
            completed_lines.append(checked.strip())
        else:
            updated_lines.append(line)

    if completed_lines:
        await obsidian.create_or_overwrite_note(
            tasks_filename, "\n".join(updated_lines) + "\n"
        )
        if notify:
            send_macos_notification(
                title="ISA Task Completed",
                subtitle=f"Completed {len(completed_lines)} task(s)",
                message=completed_lines[0],
            )
    return completed_lines


async def update_vault_task(
    obsidian: ObsidianRestClient,
    description_query: str,
    new_description: str | None = None,
    new_due_date: str | None = None,
    new_scheduled_date: str | None = None,
    new_priority: Literal["high", "medium", "low", "normal"] | None = None,
    tasks_filename: str = "Tasks.md",
    notify: bool = True,
) -> str | None:
    """Update an existing open `- [ ]` task's description, due date, scheduled date, or priority in Tasks.md."""
    if new_due_date is not None and new_due_date.strip():
        new_due_date = resolve_date_string(new_due_date)
    if new_scheduled_date is not None and new_scheduled_date.strip():
        new_scheduled_date = resolve_date_string(new_scheduled_date)

    try:
        content = await obsidian.get_note(tasks_filename)
    except Exception:
        return None

    lines = content.splitlines()
    updated_lines: list[str] = []
    modified_task_line: str | None = None

    for line in lines:
        stripped = line.strip()
        if (
            modified_task_line is None
            and stripped.startswith("- [ ]")
            and _matches_task_query(stripped, description_query)
        ):
            # Extract existing metadata from line
            base_desc = re.sub(r"^-\s*\[[ xX]\]\s*", "", stripped)
            base_desc = re.split(r"\s+(?:🔁|🛫|⏳|📅|⏫|🔼|🔽|#\w+)", base_desc)[0].strip()

            due_m = re.search(r"📅\s*(\d{4}-\d{2}-\d{2})", stripped)
            sched_m = re.search(r"⏳\s*(\d{4}-\d{2}-\d{2})", stripped)
            start_m = re.search(r"🛫\s*(\d{4}-\d{2}-\d{2})", stripped)
            rec_m = re.search(r"🔁\s*([^📅⏳🛫⏫🔼🔽#]+)", stripped)
            tags = re.findall(r"#\w+", stripped)

            existing_prio: Literal["high", "medium", "low", "normal"] = "normal"
            if "⏫" in stripped:
                existing_prio = "high"
            elif "🔼" in stripped:
                existing_prio = "medium"
            elif "🔽" in stripped:
                existing_prio = "low"

            spec = TaskSpec(
                description=(new_description.strip() if new_description else base_desc),
                due_date=(
                    new_due_date
                    if new_due_date is not None
                    else (due_m.group(1) if due_m else None)
                ),
                scheduled_date=(
                    new_scheduled_date
                    if new_scheduled_date is not None
                    else (sched_m.group(1) if sched_m else None)
                ),
                start_date=(start_m.group(1) if start_m else None),
                recurrence=(rec_m.group(1).strip() if rec_m else None),
                priority=(new_priority if new_priority is not None else existing_prio),
                tags=tags,
            )
            modified_task_line = spec.to_task_line()
            updated_lines.append(modified_task_line)
        else:
            updated_lines.append(line)

    if modified_task_line is not None:
        await obsidian.create_or_overwrite_note(
            tasks_filename, "\n".join(updated_lines) + "\n"
        )
        if notify:
            send_macos_notification(
                title="ISA Task Updated",
                subtitle="Updated in Tasks.md",
                message=modified_task_line,
            )
    return modified_task_line
