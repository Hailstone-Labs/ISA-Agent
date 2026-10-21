"""Watchdog file-system event monitor for ISA_Inbox.md with hash/timestamp debouncing."""

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


def extract_pending_command(
    content: str,
    trigger_prefix: str = "@ISA",
    require_newline: bool = True,
) -> str | None:
    """Extract an unanswered @ISA command appearing after the last agent response.

    When `require_newline=True` (default), the user must press Enter/Return after
    typing their `@ISA ...` prompt so Obsidian's mid-sentence auto-save does not
    prematurely dispatch an incomplete prompt.
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

    if require_newline and "\n" not in raw_after_trigger:
        # User is still typing on the @ISA line and hasn't pressed Enter yet
        return None

    command = raw_after_trigger.strip()
    return command if command else None


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
        self._last_trigger_time: float = 0.0
        self._last_command_hash: str | None = None

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
        self._last_trigger_time = time.monotonic()
        if dispatched_command is not None:
            self._last_command_hash = hashlib.md5(
                dispatched_command.encode("utf-8")
            ).hexdigest()

    def _check_and_dispatch(self) -> None:
        now = time.monotonic()
        if (now - self._last_trigger_time) < self.debounce_seconds:
            return

        current_hash = self._compute_hash()
        if current_hash is None or current_hash == self._last_hash:
            return

        self._last_hash = current_hash
        self._last_trigger_time = now

        try:
            content = self.inbox_path.read_text(encoding="utf-8")
        except OSError:
            return

        command = extract_pending_command(
            content,
            trigger_prefix=self.trigger_prefix,
            require_newline=self.require_newline,
        )
        if not command:
            return

        cmd_hash = hashlib.md5(command.encode("utf-8")).hexdigest()
        if cmd_hash == self._last_command_hash:
            return

        self._last_command_hash = cmd_hash
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
