"""Document synthesis, Templater instantiation, and output syntax guardrails (Sprint 5)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path, PurePosixPath
import re
from pydantic import BaseModel, Field, field_validator
import yaml
from src.monitor.observer import CURSOR_PLACEHOLDER, RESPONSE_END_MARKER
from src.tools.calendar import list_vault_calendar_events
from src.tools.notifications import send_macos_notification
from src.tools.obsidian import ObsidianRestClient
from src.tools.tasks import list_vault_tasks

OUTER_MARKDOWN_FENCE_RE = re.compile(
    r"^\s*```(?:markdown|md|yaml)?\s*\n(.*?)\n```\s*$",
    re.DOTALL | re.IGNORECASE,
)
TP_DATE_NOW_RE = re.compile(
    r"<%\s*tp\.date\.now\(\s*(?:[\"']([^\"']+)[\"'])?\s*\)\s*%>"
)
TP_FILE_TITLE_RE = re.compile(r"<%\s*tp\.file\.title\s*%>")
TP_FILE_INCLUDE_RE = re.compile(
    r"<%\s*tp\.file\.include\(\s*[\"']\[\[([^\]]+)\]\][\"']\s*\)\s*%>"
)


def validate_vault_path(vault_path: str, forbidden_files: set[str] | None = None) -> str:
    """Ensure the target vault path is relative, safe, and ends with .md."""
    cleaned = vault_path.strip().lstrip("/")
    if not cleaned:
        raise ValueError("Target vault path cannot be empty.")

    posix = PurePosixPath(cleaned)
    if ".." in posix.parts or any(p.startswith(".") for p in posix.parts):
        raise ValueError(
            f"Unsafe vault path '{vault_path}': directory traversal or hidden directories are forbidden."
        )

    if not cleaned.lower().endswith(".md"):
        cleaned = f"{cleaned}.md"

    forbidden = forbidden_files or {"ISA_Inbox.md"}
    if PurePosixPath(cleaned).name in forbidden:
        raise ValueError(
            f"Cannot overwrite protected vault file '{PurePosixPath(cleaned).name}'."
        )
    return cleaned


def validate_markdown_syntax(content: str) -> str:
    """Output guardrail: strip accidental outer code fences and validate YAML frontmatter."""
    cleaned = content.strip()
    if not cleaned:
        raise ValueError("Generated document content cannot be empty.")

    fence_match = OUTER_MARKDOWN_FENCE_RE.match(cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    if cleaned.startswith("---"):
        lines = cleaned.splitlines()
        closing_idx: int | None = None
        for idx in range(1, len(lines)):
            if lines[idx].strip() == "---":
                closing_idx = idx
                break
        if closing_idx is None:
            raise ValueError(
                "Malformed YAML frontmatter: opening '---' delimiter has no matching closing '---'."
            )
        yaml_block = "\n".join(lines[1:closing_idx])
        try:
            parsed = yaml.safe_load(yaml_block)
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML frontmatter syntax: {exc}") from exc
        if parsed is not None and not isinstance(parsed, dict):
            raise ValueError(
                "Invalid YAML frontmatter: top-level YAML structure must be a key-value mapping."
            )

    return cleaned + "\n"


def expand_templater_expressions(
    template_text: str,
    note_title: str,
    now: datetime | None = None,
) -> str:
    """Evaluate common Templater expressions (<% tp.date.now(...) %>, <% tp.file.title %>)."""
    current_dt = now or datetime.now()

    def _replace_date(match: re.Match[str]) -> str:
        fmt = match.group(1) or "YYYY-MM-DD"
        py_fmt = (
            fmt.replace("YYYY", "%Y")
            .replace("MM", "%m")
            .replace("DD", "%d")
            .replace("HH", "%H")
            .replace("mm", "%M")
            .replace("ss", "%S")
        )
        return current_dt.strftime(py_fmt)

    expanded = TP_DATE_NOW_RE.sub(_replace_date, template_text)
    expanded = TP_FILE_TITLE_RE.sub(note_title, expanded)
    return expanded


class SynthesizedDocumentSpec(BaseModel):
    """Validated specification for a synthesized Markdown document or report."""

    target_path: str = Field(
        ..., description="Vault-relative path for the new note (e.g. 'Reports/Marketing Summary.md')"
    )
    content: str = Field(
        ..., description="Full Markdown content (optionally beginning with valid YAML frontmatter)"
    )

    @field_validator("target_path")
    @classmethod
    def _check_path(cls, v: str) -> str:
        return validate_vault_path(v)

    @field_validator("content")
    @classmethod
    def _check_content(cls, v: str) -> str:
        return validate_markdown_syntax(v)


async def create_synthesized_document(
    obsidian: ObsidianRestClient,
    spec: SynthesizedDocumentSpec,
    notify: bool = True,
) -> str:
    """Validate and write a synthesized document into the Obsidian vault."""
    await obsidian.create_or_overwrite_note(spec.target_path, spec.content)
    if notify:
        send_macos_notification(
            title="ISA Document Created",
            subtitle=spec.target_path,
            message="Synthesized note saved to Obsidian vault.",
        )
    return spec.target_path


async def instantiate_template_note(
    obsidian: ObsidianRestClient,
    vault_root: Path,
    template_name: str,
    target_path: str,
    extra_body: str = "",
    templates_folder: str = "Templates",
    notify: bool = True,
) -> str:
    """Instantiate a new note from an Obsidian Templater template and trigger Templater."""
    safe_target = validate_vault_path(target_path)
    clean_template = template_name.strip()
    if clean_template.lower().endswith(".md"):
        clean_template = clean_template[:-3]

    template_file = vault_root / templates_folder / f"{clean_template}.md"
    note_title = PurePosixPath(safe_target).stem

    if template_file.exists():
        raw_template = template_file.read_text(encoding="utf-8")
        rendered = expand_templater_expressions(raw_template, note_title=note_title)
    else:
        # Use Templater's native include tag if template file is managed dynamically inside Obsidian
        rendered = (
            f'<% tp.file.include("[[{clean_template}]]") %>\n\n'
            f"# {note_title}\n"
        )

    if extra_body.strip():
        rendered = rendered.rstrip() + "\n\n" + extra_body.strip() + "\n"

    validated_payload = validate_markdown_syntax(rendered)
    await obsidian.create_or_overwrite_note(safe_target, validated_payload)

    try:
        await obsidian.execute_command("templater-obsidian:replace-in-file-templater")
    except Exception:
        pass

    if notify:
        send_macos_notification(
            title="ISA Template Instantiated",
            subtitle=f"Template: {clean_template}",
            message=f"Created {safe_target}",
        )
    return safe_target


async def generate_daily_executive_briefing(
    obsidian: ObsidianRestClient,
    vault_root: Path,
    target_date: str | None = None,
    calendar_folder: str = "Calendar",
    tasks_filename: str = "Tasks.md",
    briefings_folder: str = "Briefings",
    notify: bool = True,
) -> tuple[str, str]:
    """Generate a Daily Executive Briefing note combining today's events, open tasks, and recent notes."""
    date_str = target_date or datetime.now().strftime("%Y-%m-%d")
    all_events = list_vault_calendar_events(vault_path=vault_root, folder=calendar_folder)
    today_events = [ev for ev in all_events if ev["date"] == date_str]
    upcoming_events = [ev for ev in all_events if ev["date"] > date_str][:5]

    open_tasks = await list_vault_tasks(
        obsidian=obsidian,
        tasks_filename=tasks_filename,
        include_completed=False,
    )
    due_today_or_overdue: list[str] = []
    other_open_tasks: list[str] = []
    for t in open_tasks:
        due_m = re.search(r"📅\s*(\d{4}-\d{2}-\d{2})", t)
        if due_m and due_m.group(1) <= date_str:
            due_today_or_overdue.append(t)
        else:
            other_open_tasks.append(t)

    # Discover recent meeting notes
    meetings_dir = vault_root / "Meetings"
    recent_meetings: list[str] = []
    if meetings_dir.exists():
        md_files = sorted(
            meetings_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for mf in md_files[:3]:
            recent_meetings.append(f"- [[{mf.stem}]]")

    lines = [
        "---",
        f'title: "Daily Executive Briefing - {date_str}"',
        f"date: {date_str}",
        "type: executive-briefing",
        "tags:",
        "  - briefing",
        "  - executive",
        "---",
        f"# ☀️ Daily Executive Briefing — {date_str}",
        "",
        "## 📅 Today's Schedule",
    ]
    if today_events:
        for ev in today_events:
            time_lbl = "All Day" if ev["all_day"] else f"{ev['start_time']}–{ev['end_time']}"
            lines.append(f"- **{time_lbl}** — {ev['title']} (`{ev['file_path']}`)")
    else:
        lines.append("- *No meetings scheduled for today.*")

    lines.extend(["", "## 🔥 Priority & Due Tasks (Today / Overdue)"])
    if due_today_or_overdue:
        for t in due_today_or_overdue:
            bullet = re.sub(r"^-\s*\[[ xX]\]\s*", "- • ", t.strip())
            lines.append(bullet)
    else:
        lines.append("- *No overdue or due-today tasks.*")

    lines.extend(["", "## 📋 Other Open Tasks"])
    if other_open_tasks:
        for t in other_open_tasks:
            bullet = re.sub(r"^-\s*\[[ xX]\]\s*", "- • ", t.strip())
            lines.append(bullet)
    else:
        lines.append("- *No additional open tasks.*")

    lines.extend(["", "## 🗓️ Upcoming Calendar Horizon"])
    if upcoming_events:
        for ev in upcoming_events:
            time_lbl = "All Day" if ev["all_day"] else f"{ev['start_time']}–{ev['end_time']}"
            lines.append(f"- **{ev['date']} ({time_lbl})** — {ev['title']}")
    else:
        lines.append("- *No upcoming events on the horizon.*")

    if recent_meetings:
        lines.extend(["", "## 🤝 Recent Meeting Notes", *recent_meetings])

    briefing_md = "\n".join(lines) + "\n"
    target_path = f"{briefings_folder.strip('/')}/{date_str} - Executive Briefing.md"
    await obsidian.create_or_overwrite_note(target_path, briefing_md)

    if notify:
        send_macos_notification(
            title="ISA Executive Briefing Ready",
            subtitle=date_str,
            message=f"{len(today_events)} event(s) today, {len(open_tasks)} open task(s).",
        )
    return target_path, briefing_md


async def archive_inbox_transcript(
    obsidian: ObsidianRestClient,
    inbox_filename: str = "ISA_Inbox.md",
    archive_folder: str = "Archive",
    keep_recent_exchanges: int = 2,
    notify: bool = True,
) -> tuple[str | None, int]:
    """Archive older completed exchanges from ISA_Inbox.md into Archive/ISA_Transcript_YYYY-MM.md.

    Keeps the most recent `keep_recent_exchanges` exchanges in ISA_Inbox.md so active visual
    context remains smoothly visible while preventing inbox bloat.
    """
    try:
        content = await obsidian.get_note(inbox_filename)
    except Exception:
        return None, 0

    if RESPONSE_END_MARKER not in content:
        return None, 0

    parts = content.split(RESPONSE_END_MARKER)
    completed_blocks = parts[:-1]
    if len(completed_blocks) <= keep_recent_exchanges:
        return None, 0

    num_to_archive = len(completed_blocks) - keep_recent_exchanges
    blocks_to_archive = completed_blocks[:num_to_archive]
    blocks_to_keep = completed_blocks[num_to_archive:]

    month_tag = datetime.now().strftime("%Y-%m")
    archive_path = f"{archive_folder.strip('/')}/ISA_Transcript_{month_tag}.md"
    archive_chunk = (
        f"\n\n<!-- Archived on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} -->\n"
        + f"{RESPONSE_END_MARKER}".join(blocks_to_archive)
        + f"{RESPONSE_END_MARKER}\n"
    )

    try:
        existing_archive = await obsidian.get_note(archive_path)
        await obsidian.append_to_note(archive_path, archive_chunk)
    except Exception:
        header = (
            "---\n"
            f'title: "ISA Conversation Archive ({month_tag})"\n'
            "type: isa-archive\n"
            "---\n"
            f"# 🗄️ ISA Conversation Transcript Archive ({month_tag})\n"
        )
        await obsidian.create_or_overwrite_note(archive_path, header + archive_chunk)

    kept_body = f"{RESPONSE_END_MARKER}".join(blocks_to_keep) + f"{RESPONSE_END_MARKER}"
    new_inbox = (
        "# ISA Inbox\n\n"
        f"> *Archived {num_to_archive} older exchange(s) to [[{archive_path[:-3]}]].*\n\n"
        + kept_body.lstrip()
        + f"\n\n---\n\n@ISA {CURSOR_PLACEHOLDER}\n"
    )
    await obsidian.create_or_overwrite_note(inbox_filename, new_inbox)

    if notify:
        send_macos_notification(
            title="ISA Inbox Archived",
            subtitle=f"Moved {num_to_archive} exchange(s)",
            message=f"Saved to {archive_path}",
        )
    return archive_path, num_to_archive


async def clear_inbox_page(
    obsidian: ObsidianRestClient,
    inbox_filename: str = "ISA_Inbox.md",
    notify: bool = True,
) -> str:
    """Wipe ISA_Inbox.md back to a clean slate with a fresh @ISA prompt line."""
    clean_slate = (
        "# ISA Inbox\n\n"
        "Type commands below using `@ISA`.\n\n"
        f"@ISA {CURSOR_PLACEHOLDER}\n"
    )
    await obsidian.create_or_overwrite_note(inbox_filename, clean_slate)
    if notify:
        send_macos_notification(
            title="ISA Inbox Cleared",
            subtitle=inbox_filename,
            message="Inbox page reset to a clean slate.",
        )
    return clean_slate
