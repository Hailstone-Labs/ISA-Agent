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
COMPLETE_SENTENCE_ENDINGS = (".", "?", "!", '"', "'", ")", "]")


def extract_pending_command(
    content: str,
    trigger_prefix: str = "@ISA",
    require_newline: bool = True,
) -> str | None:
    """Extract an unanswered @ISA command appearing after the last agent response.

    When `require_newline=True` (live Watchdog mode), prevents premature mid-sentence
    triggers from Obsidian's background auto-save by requiring either:
      1. An explicit blank line (`\\n\\n`) after the `@ISA ...` prompt (pressing Enter twice), OR
      2. A newline (`\\n`) AND the prompt ending with terminal punctuation (`.`, `?`, `!`).
    """
    if THINKING_START_MARKER in content:
        # Agent is currently processing a command
        return None

    if RESPONSE_END_MARKER in content:
        trailing = content.rsplit(RESPONSE_END_MARKER, maxsplit=1)[-1]
    else:
        trailing = content

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
        # Check if user pressed Enter twice (blank line after command) or finished a sentence
        after_command_idx = raw_after_trigger.find(command) + len(command)
        trailing_whitespace = raw_after_trigger[after_command_idx:]
        has_blank_line_after = "\n\n" in trailing_whitespace or "\r\n\r\n" in trailing_whitespace
        ends_with_punctuation = command.endswith(COMPLETE_SENTENCE_ENDINGS)

        if not (has_blank_line_after or ends_with_punctuation):
            return None

    return command


class InboxEventHandler(FileSystemEventHandler):
    """Monitors the designated Obsidian inbox note and prevents self-write feedback loops."""

    def __init__(
        self,
        inbox_path: Path,
        on_user_command: Callable[[str], None],
        debounce_seconds: float = 0.5,
        trigger_prefix: str = "@ISA",
        require_newline: bool = True,
    ) -> None:
        super().__init__()
        self.inbox_path = inbox_path.resolve()
        self.on_user_command = on_user_command
        self.debounce_seconds = debounce_seconds
        self.trigger_prefix = trigger_prefix
        self.require_newline = require_newline
        self._last_hash: str | None = self._compute_hash()
        self._last_agent_write_time: float = 0.0
        self._last_command_hash: str | None = None
        self.last_user_edit_time: float = 0.0

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

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._matches_inbox(event.src_path):
            self._check_and_dispatch()

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory and self._matches_inbox(event.src_path):
            self._check_and_dispatch()

    def on_moved(self, event: FileMovedEvent) -> None:
        if not event.is_directory and self._matches_inbox(event.dest_path):
            self._check_and_dispatch()


def create_inbox_observer(
    inbox_path: Path,
    on_user_command: Callable[[str], None],
    debounce_seconds: float = 0.5,
    require_newline: bool = True,
) -> tuple[Observer, InboxEventHandler]:
    """Instantiate a watchdog Observer watching the parent directory of the inbox file."""
    inbox_path = inbox_path.resolve()
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    if not inbox_path.exists():
        inbox_path.write_text("# ISA Inbox\n\n", encoding="utf-8")

    handler = InboxEventHandler(
        inbox_path=inbox_path,
        on_user_command=on_user_command,
        debounce_seconds=debounce_seconds,
        require_newline=require_newline,
    )
    observer = Observer()
    observer.schedule(handler, str(inbox_path.parent), recursive=False)
    return observer, handler
