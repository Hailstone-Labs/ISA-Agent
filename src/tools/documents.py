"""Document synthesis, Templater instantiation, and output syntax guardrails (Sprint 5)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path, PurePosixPath
import re
from pydantic import BaseModel, Field, field_validator
import yaml
from src.tools.notifications import send_macos_notification
from src.tools.obsidian import ObsidianRestClient

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
