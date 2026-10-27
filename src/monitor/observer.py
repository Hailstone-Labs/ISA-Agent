"""Watchdog file-system event monitor for ISA_Inbox.md with trailing-edge debouncing."""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from watchdog.events import FileMovedEvent, FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

RESPONSE_END_MARKER = "<!-- ISA_RESPONSE_END -->"
THINKING_START_MARKER = "<!-- ISA_THINKING_START -->"
CURSOR_PLACEHOLDER = "<% tp.file.cursor() %>"
COMPLETE_SENTENCE_ENDINGS = (".", "?", "!", '"', "'", ")", "]")
FAST_COMMANDS = {
    "/rescan",
    "rescan",
    "/reindex",
    "reindex",
    "/briefing",
    "briefing",
    "/archive",
    "archive",
    "/clear",
    "clear",
    "/clear-inbox",
    "clear-inbox",
    "/theme",
    "theme",
    "/import-calendar",
    "import-calendar",
    "/import-ics",
    "import-ics",
    "/restart",
    "restart",
    "/reload",
    "reload",
}


def is_fast_command_str(cmd: str) -> bool:
    """Return True if command is a recognized slash command, theme switch, or calendar import."""
    low = cmd.strip().lower()
    return (
        low in FAST_COMMANDS
        or low.startswith(("/theme ", "theme "))
        or low.startswith(
            (
                "/import-calendar",
                "import-calendar",
                "/import-ics",
                "import-ics",
            )
        )
    )


def extract_pending_command(
    content: str,
    trigger_prefix: str = "@ISA",
    require_newline: bool = True,
    manual_trigger_only: bool = False,
) -> str | None:
    """Extract an unanswered @ISA command appearing after the last agent response.

    When `manual_trigger_only=True`, requires pressing the Enter key (a newline `\\n`
    after the prompt) or a fast command, completely ignoring mid-sentence or terminal
    punctuation so you can freely type commas, periods, and abbreviations without triggering.
    """
    if THINKING_START_MARKER in content:
        # Agent is currently processing a command
        return None

    if RESPONSE_END_MARKER in content:
        trailing = content.rsplit(RESPONSE_END_MARKER, maxsplit=1)[-1]
    else:
        trailing = content

    # Strip any unexpanded Templater cursor placeholder so '@ISA <% tp.file.cursor() %>' never self-triggers
    trailing = trailing.replace(CURSOR_PLACEHOLDER, "")

    if trigger_prefix.lower() not in trailing.lower():
        return None

    matches = list(
        re.finditer(
            r"^\s*(?:[-*]\s*)?@ISA\b[:\s]*",
            trailing,
            flags=re.IGNORECASE | re.MULTILINE,
        )
    )
    if not matches:
        return None

    last_match = matches[-1]
    raw_after_trigger = trailing[last_match.end() :]
    command = raw_after_trigger.strip()
    if not command:
        return None

    if require_newline:
        has_newline_after = "\n" in raw_after_trigger
        if not has_newline_after:
            return None
        # Check if user pressed Enter twice (blank line after command), finished a sentence, or ran a fast command
        after_command_idx = raw_after_trigger.find(command) + len(command)
        trailing_whitespace = raw_after_trigger[after_command_idx:]
        has_blank_line_after = "\n\n" in trailing_whitespace or "\r\n\r\n" in trailing_whitespace
        is_fast_cmd = is_fast_command_str(command)

        if manual_trigger_only:
            # Manual mode: MUST press Enter twice (blank line after prompt) or run a fast command
            if not (has_blank_line_after or is_fast_cmd):
                return None
        else:
            ends_with_punctuation = command.endswith(COMPLETE_SENTENCE_ENDINGS)
            if not (has_blank_line_after or ends_with_punctuation or is_fast_cmd):
                return None

    return command


EXCLUDED_INLINE_DIRS = {"archive", "briefings", "templates", "calendar", "imports"}
EXCLUDED_INLINE_FILENAMES = {"tasks.md"}


def extract_inline_note_command(
    content: str,
    trigger_prefix: str = "@ISA",
    manual_trigger_only: bool = False,
) -> tuple[str, int, int] | None:
    """Find an unanswered inline `@ISA <command>` line inside any vault Markdown note.

    Returns `(command_str, span_start, span_end)` so the caller can replace the inline
    `@ISA` line in-place with an `> [!NOTE] 🤖 ISA` callout block.
    """
    if THINKING_START_MARKER in content:
        return None
    if trigger_prefix.lower() not in content.lower():
        return None

    pattern = re.compile(
        r"^[ \t]*(?:[-*][ \t]*)?@ISA\b[: \t]*(?P<cmd>[^\r\n]+)",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    for match in pattern.finditer(content):
        raw_cmd = match.group("cmd").replace(CURSOR_PLACEHOLDER, "").strip()
        if not raw_cmd:
            continue

        after_line = content[match.end() :]
        if not after_line.startswith(("\n", "\r\n")):
            continue

        blank_line_match = re.match(r"^\r?\n[ \t]*\r?\n", after_line)
        has_blank_line = blank_line_match is not None
        is_fast_cmd = is_fast_command_str(raw_cmd)

        if manual_trigger_only:
            if not (has_blank_line or is_fast_cmd):
                continue
        else:
            ends_with_punct = raw_cmd.endswith(COMPLETE_SENTENCE_ENDINGS)
            if not (has_blank_line or ends_with_punct or is_fast_cmd):
                continue

        return (raw_cmd, match.start(), match.end())

    return None


class InboxEventHandler(FileSystemEventHandler):
    """Monitors the designated Obsidian inbox note and vault Markdown files."""

    def __init__(
        self,
        inbox_path: Path,
        on_user_command: Callable[[str], None],
        debounce_seconds: float = 0.5,
        trigger_prefix: str = "@ISA",
        require_newline: bool = True,
        manual_trigger_only: bool = False,
        on_vault_change: Callable[[Path], None] | None = None,
        on_inline_command: Callable[[Path, str], None] | None = None,
        calendar_import_folder: str = "Imports/Calendar",
        on_calendar_file_dropped: Callable[[Path], None] | None = None,
    ) -> None:
        super().__init__()
        self.inbox_path = inbox_path.resolve()
        self.vault_root = self.inbox_path.parent
        self.on_user_command = on_user_command
        self.debounce_seconds = debounce_seconds
        self.trigger_prefix = trigger_prefix
        self.require_newline = require_newline
        self.manual_trigger_only = manual_trigger_only
        self.on_vault_change = on_vault_change
        self.on_inline_command = on_inline_command
        self.calendar_import_folder = calendar_import_folder
        self.on_calendar_file_dropped = on_calendar_file_dropped
        self._last_hash: str | None = self._compute_hash()
        self._last_agent_write_time: float = 0.0
        self._last_command_hash: str | None = None
        self._last_inline_command_hashes: dict[Path, str] = {}
        self._last_ics_hashes: dict[Path, str] = {}
        self.last_user_edit_time: float = 0.0
        self.last_vault_edit_time: float = 0.0

    def _compute_hash(self) -> str | None:
        if not self.inbox_path.exists():
            return None
        try:
            data = self.inbox_path.read_bytes()
            return hashlib.md5(data).hexdigest()
        except OSError:
            return None

    def record_agent_write(self, dispatched_command: str | None = None) -> None:
        """Update stored MD5 hash after the agent writes to the inbox to avoid loop cascade."""
        self._last_hash = self._compute_hash()
        self._last_agent_write_time = time.monotonic()
        if dispatched_command is not None:
            self._last_command_hash = hashlib.md5(
                dispatched_command.encode("utf-8")
            ).hexdigest()

    def can_dispatch_command(self, command: str) -> bool:
        """Return True if this command has not just been dispatched."""
        cmd_hash = hashlib.md5(command.encode("utf-8")).hexdigest()
        return cmd_hash != self._last_command_hash

    def mark_command_dispatched(self, command: str) -> None:
        """Record the hash of the command being dispatched to prevent duplicate runs."""
        self._last_command_hash = hashlib.md5(command.encode("utf-8")).hexdigest()

    def _check_and_dispatch(self) -> None:
        now = time.monotonic()
        # Ignore filesystem echo events immediately following an agent write
        if (now - self._last_agent_write_time) < 0.35:
            current_hash = self._compute_hash()
            if current_hash == self._last_hash:
                return

        current_hash = self._compute_hash()
        if current_hash is None or current_hash == self._last_hash:
            return

        self._last_hash = current_hash
        self.last_user_edit_time = now

        try:
            content = self.inbox_path.read_text(encoding="utf-8")
        except OSError:
            return

        if THINKING_START_MARKER in content:
            return

        command = extract_pending_command(
            content,
            trigger_prefix=self.trigger_prefix,
            require_newline=self.require_newline,
            manual_trigger_only=self.manual_trigger_only,
        )
        if not command:
            return

        if not self.can_dispatch_command(command):
            return

        # Notify the async loop that a candidate command is ready (the async loop
        # will wait for the trailing-edge quiet period before locking & dispatching)
        self.on_user_command(command)

    def _matches_inbox(self, path_bytes_or_str: bytes | str) -> bool:
        return Path(os.fsdecode(path_bytes_or_str)).resolve() == self.inbox_path

    def _is_vault_markdown_note(self, path_bytes_or_str: bytes | str) -> Path | None:
        """Return resolved Path if the event is a non-inbox, non-hidden vault .md file."""
        p = Path(os.fsdecode(path_bytes_or_str)).resolve()
        if p == self.inbox_path or p.suffix.lower() != ".md":
            return None
        try:
            rel_parts = p.relative_to(self.vault_root).parts
        except ValueError:
            return None
        if any(part.startswith(".") for part in rel_parts):
            return None
        return p

    def _is_eligible_inline_note(self, md_path: Path) -> bool:
        """Return True if md_path is a user content note eligible for inline @ISA commands."""
        if md_path.name.lower() in EXCLUDED_INLINE_FILENAMES:
            return False
        try:
            rel_parts = md_path.relative_to(self.vault_root).parts
        except ValueError:
            return False
        if len(rel_parts) > 1 and rel_parts[0].lower() in EXCLUDED_INLINE_DIRS:
            return False
        return True

    def record_inline_agent_write(self, note_path: Path, command: str) -> None:
        """Record an inline agent write so it does not re-trigger."""
        resolved = note_path.resolve()
        self._last_inline_command_hashes[resolved] = hashlib.md5(
            command.encode("utf-8")
        ).hexdigest()
        self._last_agent_write_time = time.monotonic()

    def _check_inline_note(self, md_path: Path) -> None:
        if self.on_inline_command is None or not self._is_eligible_inline_note(md_path):
            return
        try:
            content = md_path.read_text(encoding="utf-8")
        except OSError:
            return
        extracted = extract_inline_note_command(
            content,
            trigger_prefix=self.trigger_prefix,
            manual_trigger_only=self.manual_trigger_only,
        )
        if extracted is None:
            return
        cmd, _, _ = extracted
        cmd_hash = hashlib.md5(cmd.encode("utf-8")).hexdigest()
        if self._last_inline_command_hashes.get(md_path) == cmd_hash:
            return
        self._last_inline_command_hashes[md_path] = cmd_hash
        self.on_inline_command(md_path, cmd)

    def _is_calendar_import_file(self, path_bytes_or_str: bytes | str) -> Path | None:
        """Return resolved Path if the event is an .ics file inside the calendar import folder."""
        p = Path(os.fsdecode(path_bytes_or_str)).resolve()
        if p.suffix.lower() not in {".ics", ".ical"}:
            return None
        try:
            rel_parts = p.relative_to(self.vault_root).parts
        except ValueError:
            return None
        clean_import = self.calendar_import_folder.strip("/").lower()
        import_top = clean_import.split("/")[0]
        if rel_parts and rel_parts[0].lower() == import_top:
            return p
        return None

    def _handle_fs_path(self, path_bytes_or_str: bytes | str) -> None:
        if self._matches_inbox(path_bytes_or_str):
            self._check_and_dispatch()
            return
        cal_path = self._is_calendar_import_file(path_bytes_or_str)
        if cal_path is not None and self.on_calendar_file_dropped is not None:
            if cal_path.exists():
                try:
                    cal_data = cal_path.read_bytes()
                    cal_hash = hashlib.md5(cal_data).hexdigest()
                except OSError:
                    cal_hash = None
                if cal_hash and self._last_ics_hashes.get(cal_path) != cal_hash:
                    self._last_ics_hashes[cal_path] = cal_hash
                    self.on_calendar_file_dropped(cal_path)
            return
        md_path = self._is_vault_markdown_note(path_bytes_or_str)
        if md_path is not None:
            self.last_vault_edit_time = time.monotonic()
            if self.on_vault_change is not None:
                self.on_vault_change(md_path)
            self._check_inline_note(md_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle_fs_path(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle_fs_path(event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle_fs_path(event.src_path)

    def on_moved(self, event: FileMovedEvent) -> None:
        if not event.is_directory:
            self._handle_fs_path(event.src_path)
            self._handle_fs_path(event.dest_path)


def create_inbox_observer(
    inbox_path: Path,
    on_user_command: Callable[[str], None],
    debounce_seconds: float = 0.5,
    require_newline: bool = True,
    manual_trigger_only: bool = False,
    on_vault_change: Callable[[Path], None] | None = None,
    on_inline_command: Callable[[Path, str], None] | None = None,
    calendar_import_folder: str = "Imports/Calendar",
    on_calendar_file_dropped: Callable[[Path], None] | None = None,
) -> tuple[Observer, InboxEventHandler]:
    """Instantiate a watchdog Observer watching the Obsidian vault directory recursively."""
    inbox_path = inbox_path.resolve()
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    if not inbox_path.exists():
        inbox_path.write_text("# ISA Inbox\n\n", encoding="utf-8")

    handler = InboxEventHandler(
        inbox_path=inbox_path,
        on_user_command=on_user_command,
        debounce_seconds=debounce_seconds,
        require_newline=require_newline,
        manual_trigger_only=manual_trigger_only,
        on_vault_change=on_vault_change,
        on_inline_command=on_inline_command,
        calendar_import_folder=calendar_import_folder,
        on_calendar_file_dropped=on_calendar_file_dropped,
    )
    observer = Observer()
    observer.schedule(handler, str(inbox_path.parent), recursive=True)
    return observer, handler


class SourceCodeReloadHandler(FileSystemEventHandler):
    """Watches Python source directories (`src/`, `config/`) for real content edits to trigger hot-reload."""

    def __init__(self, on_source_changed: Callable[[Path], None]) -> None:
        super().__init__()
        self.on_source_changed = on_source_changed
        self._file_hashes: dict[Path, str] = {}

    def seed_initial_hashes(self, directories: list[Path]) -> None:
        """Record baseline MD5 hashes of existing .py files so only genuine edits trigger reload."""
        for directory in directories:
            if not directory.exists():
                continue
            for py_file in directory.rglob("*.py"):
                if "__pycache__" in py_file.parts:
                    continue
                try:
                    resolved = py_file.resolve()
                    self._file_hashes[resolved] = hashlib.md5(
                        resolved.read_bytes()
                    ).hexdigest()
                except OSError:
                    pass

    def _check_py_path(self, path_bytes_or_str: bytes | str) -> None:
        p = Path(os.fsdecode(path_bytes_or_str))
        if p.suffix.lower() != ".py" or "__pycache__" in p.parts:
            return
        try:
            resolved = p.resolve()
            if not resolved.exists():
                if resolved in self._file_hashes:
                    del self._file_hashes[resolved]
                    self.on_source_changed(resolved)
                return
            new_hash = hashlib.md5(resolved.read_bytes()).hexdigest()
        except OSError:
            return

        if self._file_hashes.get(resolved) != new_hash:
            self._file_hashes[resolved] = new_hash
            self.on_source_changed(resolved)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._check_py_path(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._check_py_path(event.src_path)

    def on_moved(self, event: FileMovedEvent) -> None:
        if not event.is_directory:
            self._check_py_path(event.dest_path)


def schedule_source_code_watcher(
    observer: Observer,
    watch_dirs: list[Path],
    on_source_changed: Callable[[Path], None],
) -> SourceCodeReloadHandler:
    """Attach a SourceCodeReloadHandler to an existing Watchdog Observer for hot-reloading."""
    reload_handler = SourceCodeReloadHandler(on_source_changed=on_source_changed)
    reload_handler.seed_initial_hashes(watch_dirs)
    for d in watch_dirs:
        if d.exists() and d.is_dir():
            observer.schedule(reload_handler, str(d.resolve()), recursive=True)
    return reload_handler

