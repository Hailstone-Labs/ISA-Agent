"""Rolling context window manager retaining the last N (default 10) conversation entries."""

from __future__ import annotations

from collections import deque
from datetime import datetime
import json
import logging
from pathlib import Path
import re
from pydantic import BaseModel, Field
from src.monitor.observer import RESPONSE_END_MARKER

logger = logging.getLogger("isa.memory.context_window")

PROMPT_TRIGGER_RE = re.compile(
    r"^\s*(?:[-*]\s*)?@ISA\b[:\s]*",
    re.IGNORECASE | re.MULTILINE,
)
RESPONSE_HEADER_RE = re.compile(
    r"^>\s*\[!NOTE\]\s*ISA Response\s*(?:\(([^)]+)\))?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


class ConversationEntry(BaseModel):
    """A single user prompt and ISA response exchange."""

    user_prompt: str = Field(..., description="The user's @ISA prompt")
    assistant_response: str = Field(..., description="ISA's generated Markdown response")
    timestamp: str = Field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        description="Timestamp when the response was recorded",
    )


def parse_inbox_entries(markdown_content: str) -> list[ConversationEntry]:
    """Parse completed (@ISA prompt, ISA Response) exchanges from ISA_Inbox.md."""
    if RESPONSE_END_MARKER not in markdown_content:
        return []

    segments = markdown_content.split(RESPONSE_END_MARKER)
    completed_segments = segments[:-1]
    entries: list[ConversationEntry] = []

    for seg in completed_segments:
        resp_match = RESPONSE_HEADER_RE.search(seg)
        if not resp_match:
            continue

        timestamp = (resp_match.group(1) or "").strip() or datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        before_resp = seg[: resp_match.start()]
        after_resp = seg[resp_match.end() :]

        prompt_matches = list(PROMPT_TRIGGER_RE.finditer(before_resp))
        if not prompt_matches:
            continue
        user_prompt = before_resp[prompt_matches[-1].end() :].strip()
        if not user_prompt:
            continue

        resp_lines: list[str] = []
        for line in after_resp.strip().splitlines():
            stripped = line.strip()
            if stripped.startswith(">"):
                resp_lines.append(re.sub(r"^>\s?", "", stripped))
            else:
                resp_lines.append(line)
        assistant_response = "\n".join(resp_lines).strip()
        if assistant_response:
            entries.append(
                ConversationEntry(
                    user_prompt=user_prompt,
                    assistant_response=assistant_response,
                    timestamp=timestamp,
                )
            )

    return entries


class RollingContextWindow:
    """Fixed-capacity rolling window of the most recent conversation entries (default 10)."""

    def __init__(
        self,
        max_entries: int = 10,
        persist_path: Path | None = None,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1.")
        self.max_entries = max_entries
        self.persist_path = persist_path
        self._entries: deque[ConversationEntry] = deque(maxlen=max_entries)
        if self.persist_path and self.persist_path.exists():
            self.reload_from_disk()

    @property
    def entries(self) -> list[ConversationEntry]:
        """Return the current rolling entries ordered from oldest to newest."""
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        """Clear all entries in the rolling window and overwrite the persisted JSON file with []."""
        self._entries.clear()
        self._save_to_disk()

    def add_entry(
        self,
        user_prompt: str,
        assistant_response: str,
        timestamp: str | None = None,
    ) -> ConversationEntry:
        """Append a new entry to the rolling window, automatically evicting the oldest if > max_entries."""
        entry = ConversationEntry(
            user_prompt=user_prompt.strip(),
            assistant_response=assistant_response.strip(),
            timestamp=timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        self._entries.append(entry)
        self._save_to_disk()
        return entry

    def sync_from_inbox_markdown(self, markdown_content: str) -> int:
        """Hydrate the rolling window with the last `max_entries` exchanges from ISA_Inbox.md."""
        parsed = parse_inbox_entries(markdown_content)
        if not parsed:
            return len(self._entries)
        self._entries.clear()
        for entry in parsed[-self.max_entries :]:
            self._entries.append(entry)
        self._save_to_disk()
        return len(self._entries)

    def format_for_system_prompt(self, max_chars_per_entry: int = 800) -> str:
        """Format the rolling context window for injection into the LLM system prompt."""
        if not self._entries:
            return "Recent Conversation History: None (this is the first interaction)."

        lines: list[str] = [
            f"Recent Conversation History (Last {len(self._entries)} of max {self.max_entries} entries, oldest to newest):"
        ]
        for idx, entry in enumerate(self._entries, start=1):
            resp_preview = entry.assistant_response
            if len(resp_preview) > max_chars_per_entry:
                resp_preview = resp_preview[: max_chars_per_entry - 3] + "..."
            lines.append(
                f"[{idx}] ({entry.timestamp})\n"
                f"User: {entry.user_prompt}\n"
                f"ISA: {resp_preview}"
            )
        return "\n\n".join(lines)

    def _save_to_disk(self) -> None:
        if not self.persist_path:
            return
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            payload = [e.model_dump() for e in self._entries]
            self.persist_path.write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.debug("Could not persist rolling context window: %s", exc)

    def reload_from_disk(self) -> int:
        """Reload entries from disk, automatically resetting corrupted JSON to a clean state."""
        if not self.persist_path or not self.persist_path.exists():
            return len(self._entries)
        self._entries.clear()
        try:
            raw = json.loads(self.persist_path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("Root JSON element must be a list.")
            for item in raw[-self.max_entries :]:
                self._entries.append(ConversationEntry.model_validate(item))
        except Exception as exc:
            logger.warning(
                "Rolling context JSON at %s was corrupted (%s); resetting to clean state.",
                self.persist_path,
                exc,
            )
            self._entries.clear()
            self._save_to_disk()
        return len(self._entries)
