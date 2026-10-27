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
from src.tools.tasks import TaskSpec, append_vault_task, list_vault_tasks

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


async def _write_and_verify_vault_note(
    obsidian: ObsidianRestClient,
    vault_root: Path | None,
    rel_path: str,
    content: str,
) -> int:
    """Write a note via Obsidian REST API with local disk fallback and read-after-write verification."""
    clean_rel = rel_path.lstrip("/")
    api_wrote = False
    try:
        await obsidian.create_or_overwrite_note(clean_rel, content)
        api_wrote = True
    except Exception:
        if vault_root is None:
            raise

    if vault_root is not None:
        local_file = (vault_root / clean_rel).resolve()
        if not api_wrote or not local_file.exists():
            local_file.parent.mkdir(parents=True, exist_ok=True)
            local_file.write_text(content, encoding="utf-8")
        if not local_file.exists() or local_file.stat().st_size == 0:
            raise OSError(f"Read-after-write verification failed: '{clean_rel}' was not persisted to disk.")

    return len(content.encode("utf-8"))


def resolve_existing_vault_note(
    vault_root: Path,
    note_path_or_query: str,
    forbidden_files: set[str] | None = None,
) -> str | None:
    """Resolve a user or LLM note name/path (e.g. 'SET Meeting' or 'Reports/Blog Overview.md') to an existing vault-relative path."""
    cleaned = note_path_or_query.strip().lstrip("/")
    if not cleaned:
        return None
    forbidden = {f.lower() for f in (forbidden_files or {"ISA_Inbox.md"})}

    direct_rel = cleaned if cleaned.lower().endswith(".md") else f"{cleaned}.md"
    direct_path = (vault_root / direct_rel).resolve()
    if direct_path.exists() and direct_path.is_file():
        if direct_path.name.lower() not in forbidden:
            return direct_path.relative_to(vault_root.resolve()).as_posix()

    query_stem = PurePosixPath(direct_rel).stem.lower()
    exact_stem_matches: list[Path] = []
    fuzzy_matches: list[Path] = []

    for p in sorted(vault_root.rglob("*.md")):
        try:
            rel_parts = p.relative_to(vault_root).parts
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel_parts):
            continue
        if p.name.lower() in forbidden:
            continue
        if len(rel_parts) > 1 and rel_parts[0].lower() == "archive":
            continue
        stem_low = p.stem.lower()
        if stem_low == query_stem:
            exact_stem_matches.append(p)
        elif query_stem in stem_low or stem_low in query_stem:
            fuzzy_matches.append(p)

    chosen = exact_stem_matches[0] if exact_stem_matches else (fuzzy_matches[0] if fuzzy_matches else None)
    if chosen is not None:
        return chosen.resolve().relative_to(vault_root.resolve()).as_posix()
    return None


async def create_synthesized_document(
    obsidian: ObsidianRestClient,
    spec: SynthesizedDocumentSpec,
    notify: bool = True,
    vault_root: Path | None = None,
) -> str:
    """Validate, write, and verify a synthesized document in the Obsidian vault."""
    await _write_and_verify_vault_note(
        obsidian=obsidian,
        vault_root=vault_root,
        rel_path=spec.target_path,
        content=spec.content,
    )
    if notify:
        send_macos_notification(
            title="ISA Document Created",
            subtitle=spec.target_path,
            message="Synthesized note saved and verified in Obsidian vault.",
        )
    return spec.target_path


async def edit_vault_document(
    obsidian: ObsidianRestClient,
    vault_root: Path,
    target_path: str,
    append_text: str | None = None,
    target_text: str | None = None,
    replacement_text: str | None = None,
    new_content: str | None = None,
    section_heading: str | None = None,
    notify: bool = True,
) -> tuple[str, int]:
    """Edit or append to an existing Markdown note in the vault with read-after-write verification.

    Returns `(resolved_vault_path, verified_byte_size)`.
    """
    resolved_rel = resolve_existing_vault_note(vault_root, target_path)
    if resolved_rel is None:
        # If note doesn't exist yet and caller provided new_content or append_text, validate path and create it
        resolved_rel = validate_vault_path(target_path)

    existing_content = ""
    try:
        existing_content = await obsidian.get_note(resolved_rel)
    except Exception:
        local_p = vault_root / resolved_rel
        if local_p.exists():
            existing_content = local_p.read_text(encoding="utf-8")

    if not existing_content:
        # Creating a brand-new note from the provided content
        initial_body = new_content or append_text or replacement_text or ""
        validated = validate_markdown_syntax(initial_body)
        byte_len = await _write_and_verify_vault_note(
            obsidian=obsidian,
            vault_root=vault_root,
            rel_path=resolved_rel,
            content=validated,
        )
        if notify:
            send_macos_notification(
                title="ISA Document Created",
                subtitle=resolved_rel,
                message=f"Verified on disk ({byte_len} bytes).",
            )
        return resolved_rel, byte_len

    updated_content = existing_content

    if target_text and target_text.strip() and replacement_text is not None:
        if target_text in existing_content:
            updated_content = existing_content.replace(target_text, replacement_text)
        else:
            # Case-insensitive fallback search
            pattern = re.compile(re.escape(target_text.strip()), flags=re.IGNORECASE)
            if pattern.search(existing_content):
                updated_content = pattern.sub(replacement_text, existing_content, count=1)
            else:
                raise ValueError(
                    f"Target text '{target_text[:60]}' was not found in `{resolved_rel}`. "
                    f"Read the note first or use `append_text` / `new_content`."
                )
    elif append_text and append_text.strip():
        clean_addition = append_text.strip()
        if section_heading and section_heading.strip():
            clean_heading = section_heading.strip().lstrip("#").strip()
            heading_re = re.compile(
                rf"(^#+\s*{re.escape(clean_heading)}\s*$\n?)",
                flags=re.IGNORECASE | re.MULTILINE,
            )
            if heading_re.search(existing_content):
                updated_content = heading_re.sub(
                    rf"\1\n{clean_addition}\n",
                    existing_content,
                    count=1,
                )
            else:
                heading_prefix = section_heading.strip() if section_heading.strip().startswith("#") else f"## {clean_heading}"
                updated_content = existing_content.rstrip() + f"\n\n{heading_prefix}\n\n{clean_addition}\n"
        else:
            updated_content = existing_content.rstrip() + f"\n\n{clean_addition}\n"
    elif new_content and new_content.strip():
        cleaned_new = validate_markdown_syntax(new_content)
        # Preserve existing YAML frontmatter if existing note had frontmatter and new_content omitted it
        if existing_content.startswith("---") and not cleaned_new.startswith("---"):
            parts = existing_content.split("---", 2)
            if len(parts) >= 3:
                updated_content = f"---{parts[1]}---\n{cleaned_new}"
            else:
                updated_content = cleaned_new
        else:
            updated_content = cleaned_new
    else:
        raise ValueError(
            "Must provide `append_text`, `target_text` + `replacement_text`, or `new_content` to edit a document."
        )

    validated_final = validate_markdown_syntax(updated_content)
    byte_len = await _write_and_verify_vault_note(
        obsidian=obsidian,
        vault_root=vault_root,
        rel_path=resolved_rel,
        content=validated_final,
    )
    if notify:
        send_macos_notification(
            title="ISA Document Updated",
            subtitle=resolved_rel,
            message=f"Verified update on disk ({byte_len} bytes).",
        )
    return resolved_rel, byte_len


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
    await _write_and_verify_vault_note(
        obsidian=obsidian,
        vault_root=vault_root,
        rel_path=safe_target,
        content=validated_payload,
    )

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


ACTION_ITEM_PREFIX_RE = re.compile(
    r"^\s*(?:[-*]\s*(?:\[\s*\]\s*)?)?(?:ACTION(?:\s+ITEM)?|TODO|TASK|NEXT\s+STEP|FOLLOW[- ]?UP)\s*[:\-—]\s*(.+)$",
    re.IGNORECASE,
)
ACTION_VERB_BULLET_RE = re.compile(
    r"^\s*[-*]\s+(?:\[\s*\]\s*)?((?:review|send|schedule|draft|prepare|submit|call|email|update|create|follow\s+up|finalize|complete|check|share|sync|organize|confirm|investigate|write)\b.+)$",
    re.IGNORECASE,
)
UNCHECKED_BOX_RE = re.compile(r"^\s*-\s*\[\s\]\s+(.+)$")


def parse_action_items_from_markdown(markdown_text: str) -> list[str]:
    """Deterministically extract actionable items from meeting notes or discussion bullets."""
    extracted: list[str] = []
    seen_low: set[str] = set()

    def _add(item_str: str) -> None:
        cleaned = re.sub(r"^-\s*\[[ xX]\]\s*", "", item_str.strip()).strip()
        if not cleaned:
            return
        low = cleaned.lower()
        if low not in seen_low:
            seen_low.add(low)
            extracted.append(cleaned)

    in_action_section = False
    for raw_line in markdown_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith(">") or re.match(r"^(?:[-*]\s*)?@ISA\b", stripped, re.IGNORECASE):
            continue
        if stripped.startswith("#"):
            heading_low = stripped.lstrip("#").strip().lower()
            in_action_section = any(
                kw in heading_low
                for kw in ("action item", "next step", "todo", "to-do", "deliverable")
            )
            continue

        m_prefix = ACTION_ITEM_PREFIX_RE.match(stripped)
        if m_prefix:
            _add(m_prefix.group(1))
            continue

        m_box = UNCHECKED_BOX_RE.match(stripped)
        if m_box:
            _add(m_box.group(1))
            continue

        if in_action_section and stripped.startswith(("-", "*")):
            bullet_text = re.sub(r"^[-*]\s*(?:•\s*)?", "", stripped).strip()
            if bullet_text:
                _add(bullet_text)
            continue

        m_verb = ACTION_VERB_BULLET_RE.match(stripped)
        if m_verb:
            _add(m_verb.group(1))

    return extracted


async def sync_meeting_action_items(
    obsidian: ObsidianRestClient,
    vault_root: Path,
    note_query: str,
    explicit_items: list[str] | None = None,
    due_date: str | None = None,
    default_due_date: str | None = None,
    tasks_filename: str = "Tasks.md",
    notify: bool = True,
) -> tuple[str, list[str], int]:
    """Extract or record action items in a meeting note and sync them to Tasks.md without duplicates.

    Notice that inside the meeting note itself, action items are formatted as `- •` bullets
    (and synced into Tasks.md as `- [ ]`) so Obsidian Tasks never double-counts the same item
    from both the meeting note and Tasks.md.
    """
    resolved_due = due_date or default_due_date
    clean_q = note_query.strip().lstrip("/")
    candidates: list[Path] = []
    direct = vault_root / (clean_q if clean_q.endswith(".md") else f"{clean_q}.md")
    if direct.exists():
        candidates.append(direct)
    else:
        q_low = Path(clean_q).stem.lower()
        for p in sorted(vault_root.rglob("*.md")):
            if any(part.startswith(".") for part in p.relative_to(vault_root).parts):
                continue
            if q_low in p.stem.lower():
                candidates.append(p)

    if not candidates:
        raise ValueError(f"No meeting note matched '{note_query}'.")

    target_file = candidates[0]
    rel_path = str(target_file.relative_to(vault_root))
    note_content = await obsidian.get_note(rel_path)

    items = [i.strip() for i in (explicit_items or []) if i and i.strip()]
    if not items:
        items = parse_action_items_from_markdown(note_content)

    if not items:
        return rel_path, [], 0

    added_lines: list[str] = []
    skipped_count = 0
    for desc in items:
        clean_desc = re.sub(r"^-\s*(?:\[[ xX]\]|•)\s*", "", desc).strip()
        spec = TaskSpec(
            description=f"{clean_desc} (from [[{target_file.stem}]])"
            if f"[[{target_file.stem}]]" not in clean_desc
            else clean_desc,
            due_date=resolved_due,
        )
        task_line, created_new = await append_vault_task(
            obsidian=obsidian,
            task=spec,
            tasks_filename=tasks_filename,
            notify=False,
        )
        if created_new:
            added_lines.append(task_line)
        else:
            skipped_count += 1

    # Convert any raw `- [ ]` lines in the meeting note to `- •` and update '## ⚡ Action Items'
    # so Obsidian Tasks only indexes the single canonical entry in Tasks.md
    sanitized_note = re.sub(
        r"^(\s*-\s*)\[\s\]\s+",
        r"\1• ",
        note_content,
        flags=re.MULTILINE,
    )
    bullet_prefix_re = re.compile(r"^-\s*(?:\[[ xX]\]|•)\s*")
    bullet_block = "\n".join(
        f"- • {bullet_prefix_re.sub('', i).strip()}" for i in items
    )
    if re.search(r"^##+\s*.*Action Items.*$", sanitized_note, flags=re.IGNORECASE | re.MULTILINE):
        updated_note = re.sub(
            r"(^##+\s*.*Action Items.*$\n?)(?:[\s\S]*?)(?=\n##+\s|\n@ISA\b|\n>\s*\[!NOTE\]|\Z)",
            r"\1" + bullet_block + "\n",
            sanitized_note,
            count=1,
            flags=re.IGNORECASE | re.MULTILINE,
        )
    else:
        updated_note = sanitized_note.rstrip() + f"\n\n## ⚡ Action Items\n{bullet_block}\n"

    await obsidian.create_or_overwrite_note(rel_path, updated_note)

    if notify and added_lines:
        send_macos_notification(
            title="ISA Action Items Synced",
            subtitle=target_file.stem,
            message=f"Added {len(added_lines)} new task(s) to {tasks_filename}",
        )
    return rel_path, added_lines, skipped_count
