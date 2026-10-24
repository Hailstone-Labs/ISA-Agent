"""Active local reminder scanner and native macOS Notification Center integration."""

from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
import re
import subprocess
import yaml

logger = logging.getLogger("isa.notifications")

UNCHECKED_TASK_DUE_REGEX = re.compile(
    r"^\s*-\s*\[\s\]\s*(?P<desc>.+?)\s*📅\s*(?P<due>\d{4}-\d{2}-\d{2})"
)
FRONTMATTER_REGEX = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)


def send_macos_notification(
    title: str,
    message: str,
    subtitle: str = "",
    sound: str = "Glass",
) -> bool:
    """Trigger a native macOS Notification Center banner via osascript."""
    esc_title = title.replace("\\", "\\\\").replace('"', '\\"')
    esc_msg = message.replace("\\", "\\\\").replace('"', '\\"')
    esc_sub = subtitle.replace("\\", "\\\\").replace('"', '\\"')

    script = f'display notification "{esc_msg}" with title "{esc_title}"'
    if esc_sub:
        script += f' subtitle "{esc_sub}"'
    if sound:
        script += f' sound name "{sound}"'

    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        return True
    except Exception as exc:
        logger.debug("macOS notification failed: %s", exc)
        return False


class ActiveReminderMonitor:
    """Scans Obsidian Calendar notes and Tasks.md to fire native macOS alerts when due."""

    def __init__(
        self,
        vault_path: Path,
        calendar_folder: str = "Calendar",
        tasks_filename: str = "Tasks.md",
        lead_minutes: int = 15,
    ) -> None:
        self.vault_path = vault_path
        self.calendar_dir = vault_path / calendar_folder
        self.tasks_path = vault_path / tasks_filename
        self.lead_minutes = lead_minutes
        self._notified_keys: set[str] = set()

    def scan_due_tasks(self, now: datetime | None = None) -> list[tuple[str, str]]:
        """Return (task_description, due_date_str) for unchecked tasks due today or earlier."""
        current_dt = now or datetime.now()
        today_str = current_dt.strftime("%Y-%m-%d")
        due_items: list[tuple[str, str]] = []

        if not self.tasks_path.exists():
            return due_items

        try:
            lines = self.tasks_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return due_items

        for line in lines:
            match = UNCHECKED_TASK_DUE_REGEX.search(line)
            if not match:
                continue
            due_str = match.group("due")
            raw_desc = match.group("desc")
            # Strip inline recurrence/priority emojis for clean display
            clean_desc = re.sub(r"[🔁🛫⏳⏫🔼🔽].*$", "", raw_desc).strip()
            if due_str <= today_str:
                due_items.append((clean_desc, due_str))

        return due_items

    def scan_upcoming_events(self, now: datetime | None = None) -> list[tuple[str, str, str]]:
        """Return (title, date_str, start_time_str) for calendar events starting within lead_minutes."""
        current_dt = now or datetime.now()
        today_str = current_dt.strftime("%Y-%m-%d")
        upcoming: list[tuple[str, str, str]] = []

        if not self.calendar_dir.exists():
            return upcoming

        for md_file in sorted(self.calendar_dir.glob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8")
            except OSError:
                continue
            fm_match = FRONTMATTER_REGEX.match(content)
            if not fm_match:
                continue
            try:
                meta = yaml.safe_load(fm_match.group(1)) or {}
            except Exception:
                continue

            event_date = str(meta.get("date", "")).strip()
            if event_date != today_str:
                continue

            title = str(meta.get("title", md_file.stem)).strip()
            if meta.get("allDay") is True:
                upcoming.append((title, event_date, "All Day"))
                continue

            raw_start = meta.get("startTime", "")
            if isinstance(raw_start, int):
                start_time_str = f"{raw_start // 60:02d}:{raw_start % 60:02d}"
            else:
                start_time_str = str(raw_start).strip()
            if not start_time_str:
                continue

            try:
                event_dt = datetime.strptime(
                    f"{event_date} {start_time_str}", "%Y-%m-%d %H:%M"
                )
            except ValueError:
                continue

            delta_minutes = (event_dt - current_dt).total_seconds() / 60.0
            if -5.0 <= delta_minutes <= self.lead_minutes:
                upcoming.append((title, event_date, start_time_str))

        return upcoming

    def check_and_notify(self, now: datetime | None = None) -> int:
        """Scan due tasks and upcoming events and fire macOS notifications for new alerts."""
        fired = 0
        for desc, due_str in self.scan_due_tasks(now=now):
            key = f"task::{due_str}::{desc}"
            if key in self._notified_keys:
                continue
            self._notified_keys.add(key)
            send_macos_notification(
                title="ISA Task Reminder",
                subtitle=f"Due: {due_str}",
                message=desc,
            )
            fired += 1

        for title, date_str, start_time in self.scan_upcoming_events(now=now):
            key = f"event::{date_str}::{start_time}::{title}"
            if key in self._notified_keys:
                continue
            self._notified_keys.add(key)
            send_macos_notification(
                title="ISA Calendar Alert",
                subtitle=f"Today at {start_time}",
                message=title,
            )
            fired += 1

        return fired

    def should_trigger_morning_briefing(
        self,
        target_time_hhmm: str = "08:00",
        now: datetime | None = None,
    ) -> bool:
        """Return True once per day when current local time >= target_time_hhmm and today's briefing doesn't exist yet."""
        clean_time = target_time_hhmm.strip()
        if not clean_time or not re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", clean_time):
            return False

        current_dt = now or datetime.now()
        today_str = current_dt.strftime("%Y-%m-%d")
        key = f"briefing::{today_str}"
        if key in self._notified_keys:
            return False

        briefing_file = self.vault_path / "Briefings" / f"{today_str} - Executive Briefing.md"
        if briefing_file.exists():
            self._notified_keys.add(key)
            return False

        current_hhmm = current_dt.strftime("%H:%M")
        if current_hhmm >= clean_time:
            self._notified_keys.add(key)
            return True

        return False
