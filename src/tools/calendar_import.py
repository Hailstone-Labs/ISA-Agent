"""iCalendar (.ics) parser and importer for Outlook and Microsoft Teams events into Obsidian Full Calendar."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import hashlib
import logging
from pathlib import Path
import re
from typing import Literal
import dateutil.parser
import dateutil.rrule
import dateutil.tz
import yaml

from src.tools.calendar import (
    CalendarEventSpec,
    UNSAFE_FILENAME_CHARS,
    _extract_calendar_body,
    _parse_calendar_frontmatter,
)
from src.tools.notifications import send_macos_notification
from src.tools.obsidian import ObsidianRestClient

logger = logging.getLogger("isa.calendar_import")

TEAMS_JOIN_URL_REGEX = re.compile(
    r"https://(?:teams\.microsoft\.com/l/meetup-join/[^\s<>\"'\)]+|teams\.live\.com/meet/[^\s<>\"'\)]+)",
    re.IGNORECASE,
)

HTML_TAG_REGEX = re.compile(r"<[^>]+>")
HTML_BR_REGEX = re.compile(r"<br\s*/?>", re.IGNORECASE)
HTML_P_REGEX = re.compile(r"</p>", re.IGNORECASE)


def unfold_ical_lines(content: str) -> list[str]:
    """Unfold RFC 5545 lines where continuation lines begin with whitespace."""
    raw_lines = content.splitlines()
    unfolded: list[str] = []
    for line in raw_lines:
        line_stripped = line.rstrip("\r\n")
        if not line_stripped:
            continue
        if (line_stripped.startswith(" ") or line_stripped.startswith("\t")) and unfolded:
            unfolded[-1] += line_stripped[1:]
        else:
            unfolded.append(line_stripped)
    return unfolded


def unescape_ical_text(text: str) -> str:
    """Unescape RFC 5545 escaped characters (\\n, \\,, \\;, \\\\)."""
    text = re.sub(r"\\+n", "\n", text, flags=re.IGNORECASE)
    return (
        text.replace(r"\,", ",")
        .replace(r"\;", ";")
        .replace(r"\\", "\\")
    )


def clean_html_description(text: str) -> str:
    """Clean rich-text HTML descriptions often produced by Outlook/Exchange exports."""
    if "<" not in text or ">" not in text:
        return text.strip()
    s = HTML_BR_REGEX.sub("\n", text)
    s = HTML_P_REGEX.sub("\n\n", s)
    s = HTML_TAG_REGEX.sub("", s)
    s = (
        s.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    # Collapse multiple consecutive blank lines
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def extract_teams_join_url(text: str) -> str | None:
    """Extract Microsoft Teams meeting join URL from text."""
    if not text:
        return None
    match = TEAMS_JOIN_URL_REGEX.search(text)
    if match:
        return match.group(0).rstrip(".,;>")
    return None


def parse_ical_param_dict(param_str: str) -> dict[str, str]:
    """Parse parameter key-value pairs from property names, e.g. 'DTSTART;TZID=America/Chicago'."""
    params: dict[str, str] = {}
    tokens = param_str.split(";")
    for token in tokens[1:]:
        if "=" in token:
            k, v = token.split("=", 1)
            params[k.strip().upper()] = v.strip().strip('"')
    return params


def parse_ical_datetime(
    val: str,
    params: dict[str, str] | None = None,
) -> tuple[datetime, bool]:
    """Parse RFC 5545 date or datetime string into a local datetime object and all-day flag.

    Returns:
        tuple[datetime, bool]: (normalized_local_datetime, is_all_day)
    """
    params = params or {}
    val_clean = val.strip()

    # Check for DATE value (all-day event)
    is_date = (
        params.get("VALUE") == "DATE"
        or (len(val_clean) == 8 and val_clean.isdigit())
    )
    if is_date:
        try:
            d = datetime.strptime(val_clean[:8], "%Y%m%d")
            return d, True
        except ValueError:
            pass

    # Check for UTC datetime (ending in Z)
    if val_clean.endswith("Z"):
        clean_no_z = val_clean[:-1]
        try:
            dt_utc = datetime.strptime(clean_no_z, "%Y%m%dT%H%M%S")
        except ValueError:
            dt_utc = dateutil.parser.parse(val_clean)
        dt_utc = dt_utc.replace(tzinfo=dateutil.tz.tzutc())
        local_dt = dt_utc.astimezone(dateutil.tz.tzlocal())
        return local_dt.replace(tzinfo=None), False

    # Check for TZID parameter
    tzid = params.get("TZID")
    if tzid:
        tz = dateutil.tz.gettz(tzid)
        try:
            dt_naive = datetime.strptime(val_clean, "%Y%m%dT%H%M%S")
        except ValueError:
            dt_naive = dateutil.parser.parse(val_clean)
        if tz is not None:
            dt_aware = dt_naive.replace(tzinfo=tz)
            local_dt = dt_aware.astimezone(dateutil.tz.tzlocal())
            return local_dt.replace(tzinfo=None), False
        return dt_naive, False

    # Floating local time (no timezone specified)
    try:
        dt = datetime.strptime(val_clean, "%Y%m%dT%H%M%S")
        return dt, False
    except ValueError:
        parsed = dateutil.parser.parse(val_clean)
        return parsed, False


@dataclass
class ParsedCalendarEvent:
    """Normalized calendar event representation ready for Obsidian Full Calendar generation."""

    uid: str
    title: str
    date: str
    end_date: str
    all_day: bool
    start_time: str | None = None
    end_time: str | None = None
    location: str = ""
    teams_join_url: str | None = None
    description: str = ""
    attendees: list[str] = field(default_factory=list)
    organizer: str | None = None
    status: str | None = None
    source: str = "outlook"

    def safe_filename(self, folder: str = "Calendar") -> str:
        """Return the vault-relative path for this event note."""
        clean_title = UNSAFE_FILENAME_CHARS.sub("-", self.title).strip()
        clean_folder = folder.strip("/")
        if clean_folder:
            return f"{clean_folder}/{self.date} - {clean_title}.md"
        return f"{self.date} - {clean_title}.md"

    def to_calendar_spec(self) -> CalendarEventSpec:
        """Convert into standard CalendarEventSpec."""
        agenda_body = self.render_body()
        return CalendarEventSpec(
            title=self.title,
            date=self.date,
            end_date=self.end_date,
            all_day=self.all_day,
            start_time=self.start_time,
            end_time=self.end_time,
            event_type="single",
            agenda_or_notes=agenda_body,
            uid=self.uid,
            source=self.source,
        )

    def render_body(self) -> str:
        """Render the Markdown body below YAML frontmatter."""
        body_parts: list[str] = []

        if self.teams_join_url:
            body_parts.append(
                f"[🔗 Join Microsoft Teams Meeting]({self.teams_join_url})\n"
            )

        meta_lines: list[str] = []
        if self.location and not (self.teams_join_url and "teams.microsoft.com" in self.location):
            meta_lines.append(f"**Location:** {self.location}")
        if self.organizer:
            meta_lines.append(f"**Organizer:** {self.organizer}")
        if self.attendees:
            meta_lines.append(f"**Attendees:** {', '.join(self.attendees)}")
        if self.status and self.status.upper() != "CONFIRMED":
            meta_lines.append(f"**Status:** {self.status.title()}")

        if meta_lines:
            body_parts.append("\n".join(meta_lines) + "\n")

        cleaned_desc = clean_html_description(self.description)
        if cleaned_desc:
            body_parts.append("### Agenda / Notes\n" + cleaned_desc)

        return "\n".join(body_parts).strip()

    def to_markdown(self) -> str:
        """Render full Obsidian Full Calendar note with frontmatter and body."""
        return self.to_calendar_spec().to_markdown()


@dataclass
class CalendarImportResult:
    """Result summary of a calendar import run."""

    created: int = 0
    updated: int = 0
    skipped: int = 0
    total_events: int = 0
    imported_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def changed_count(self) -> int:
        return self.created + self.updated

    def summary_markdown(self, filename: str = "") -> str:
        name_str = f" from `{filename}`" if filename else ""
        lines = [
            f"📅 **Calendar Import Complete**{name_str}\n",
            f"- **New Events Created:** {self.created}",
            f"- **Existing Events Updated:** {self.updated}",
            f"- **Unchanged (Skipped):** {self.skipped}",
            f"- **Total Events Processed:** {self.total_events}",
        ]
        if self.imported_files:
            lines.append("\n**Events:**")
            for f in self.imported_files[:10]:
                lines.append(f"- `{f}`")
            if len(self.imported_files) > 10:
                lines.append(f"- ... and {len(self.imported_files) - 10} more")
        if self.errors:
            lines.append("\n**Warnings/Errors:**")
            for err in self.errors[:5]:
                lines.append(f"- ⚠️ {err}")
        return "\n".join(lines)


def _extract_attendee_cn(val: str, params: dict[str, str]) -> str:
    """Extract common name or email from an ATTENDEE or ORGANIZER property."""
    cn = params.get("CN")
    email = val.strip()
    if email.lower().startswith("mailto:"):
        email = email[7:].strip()
    if cn and email and cn.lower() != email.lower():
        return f"{cn} ({email})"
    return cn or email


def parse_vevent_block(
    vevent_lines: list[str],
    horizon_days: int = 60,
) -> list[ParsedCalendarEvent]:
    """Parse a single VEVENT block lines into one or more ParsedCalendarEvent instances."""
    props: dict[str, tuple[str, dict[str, str]]] = {}
    attendees: list[str] = []
    rrule_str: str | None = None

    for line in vevent_lines:
        if ":" not in line:
            continue
        head, val = line.split(":", 1)
        prop_tokens = head.split(";")
        prop_name = prop_tokens[0].strip().upper()
        params = parse_ical_param_dict(head)

        if prop_name == "ATTENDEE":
            att_name = _extract_attendee_cn(val, params)
            if att_name:
                attendees.append(att_name)
        elif prop_name == "RRULE":
            rrule_str = val.strip()
        else:
            props[prop_name] = (val, params)

    summary_raw = props.get("SUMMARY", ("Untitled Meeting", {}))[0]
    title = unescape_ical_text(summary_raw).strip() or "Untitled Meeting"

    uid_raw = props.get("UID", ("", {}))[0].strip()
    dtstart_data = props.get("DTSTART")
    if not dtstart_data:
        logger.debug("Skipping VEVENT with no DTSTART: %s", title)
        return []

    dtstart_val, dtstart_params = dtstart_data
    try:
        start_dt, all_day = parse_ical_datetime(dtstart_val, dtstart_params)
    except Exception as exc:
        logger.warning("Could not parse DTSTART '%s' for event '%s': %s", dtstart_val, title, exc)
        return []

    # Parse DTEND or DURATION
    end_dt: datetime = start_dt
    if "DTEND" in props:
        dtend_val, dtend_params = props["DTEND"]
        try:
            end_dt, _ = parse_ical_datetime(dtend_val, dtend_params)
        except Exception:
            end_dt = start_dt
    elif "DURATION" in props:
        # Simple duration parsing (e.g. PT1H, PT30M, P1D)
        dur_str = props["DURATION"][0].strip()
        dur_delta = timedelta(hours=1)
        try:
            m = re.match(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", dur_str)
            if m:
                days = int(m.group(1) or 0)
                hours = int(m.group(2) or 0)
                minutes = int(m.group(3) or 0)
                seconds = int(m.group(4) or 0)
                dur_delta = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
        except Exception:
            pass
        end_dt = start_dt + dur_delta
    else:
        # Default end
        end_dt = start_dt if all_day else (start_dt + timedelta(hours=1))

    if not uid_raw:
        # Deterministic UID fallback
        uid_raw = hashlib.sha256(f"{title}_{start_dt.isoformat()}".encode()).hexdigest()[:24]

    location_raw = unescape_ical_text(props.get("LOCATION", ("", {}))[0]).strip()
    raw_desc = unescape_ical_text(props.get("DESCRIPTION", ("", {}))[0]).strip()
    # Extract Teams URL before stripping HTML tags (in case URL is in an <a href="..."> tag)
    teams_url = None
    if "X-MICROSOFT-SKYPETEAMSMEETINGURL" in props:
        teams_url = props["X-MICROSOFT-SKYPETEAMSMEETINGURL"][0].strip()
    if not teams_url:
        teams_url = extract_teams_join_url(raw_desc) or extract_teams_join_url(location_raw)

    desc_clean = clean_html_description(raw_desc)
    status_raw = props.get("STATUS", ("", {}))[0].strip() or None

    organizer_data = props.get("ORGANIZER")
    organizer: str | None = None
    if organizer_data:
        organizer = _extract_attendee_cn(organizer_data[0], organizer_data[1])

    duration = end_dt - start_dt

    # Handle recurrence expansion
    now = datetime.now()
    window_start = now - timedelta(days=7)
    window_end = now + timedelta(days=horizon_days)

    occurrences: list[tuple[datetime, datetime, str]] = []
    if rrule_str:
        try:
            rule = dateutil.rrule.rrulestr(rrule_str, dtstart=start_dt)
            rule_occurrences = rule.between(window_start, window_end, inc=True)
            for i, occ_start in enumerate(rule_occurrences[:100]):
                occ_end = occ_start + duration
                occ_uid = f"{uid_raw}_{occ_start.strftime('%Y%m%d')}"
                occurrences.append((occ_start, occ_end, occ_uid))
        except Exception as exc:
            logger.warning("Error evaluating RRULE '%s' for event '%s': %s", rrule_str, title, exc)
            occurrences.append((start_dt, end_dt, uid_raw))
    else:
        occurrences.append((start_dt, end_dt, uid_raw))

    results: list[ParsedCalendarEvent] = []
    for occ_start, occ_end, occ_uid in occurrences:
        start_date_str = occ_start.strftime("%Y-%m-%d")

        if all_day:
            # In RFC 5545, all-day DTEND is exclusive (the day after).
            # If start is 2026-10-25 and end is 2026-10-26, it is a single-day event on 2026-10-25.
            if occ_end > occ_start:
                inclusive_end = occ_end - timedelta(days=1)
                end_date_str = max(start_date_str, inclusive_end.strftime("%Y-%m-%d"))
            else:
                end_date_str = start_date_str

            results.append(
                ParsedCalendarEvent(
                    uid=occ_uid,
                    title=title,
                    date=start_date_str,
                    end_date=end_date_str,
                    all_day=True,
                    start_time=None,
                    end_time=None,
                    location=location_raw,
                    teams_join_url=teams_url,
                    description=desc_clean,
                    attendees=attendees,
                    organizer=organizer,
                    status=status_raw,
                )
            )
        else:
            end_date_str = occ_end.strftime("%Y-%m-%d")
            start_time_str = occ_start.strftime("%H:%M")
            end_time_str = occ_end.strftime("%H:%M")

            results.append(
                ParsedCalendarEvent(
                    uid=occ_uid,
                    title=title,
                    date=start_date_str,
                    end_date=end_date_str,
                    all_day=False,
                    start_time=start_time_str,
                    end_time=end_time_str,
                    location=location_raw,
                    teams_join_url=teams_url,
                    description=desc_clean,
                    attendees=attendees,
                    organizer=organizer,
                    status=status_raw,
                )
            )

    return results


def parse_ics_content(content: str, horizon_days: int = 60) -> list[ParsedCalendarEvent]:
    """Parse full iCalendar (.ics) string content into a list of ParsedCalendarEvent."""
    unfolded = unfold_ical_lines(content)
    events: list[ParsedCalendarEvent] = []

    inside_vevent = False
    current_vevent_lines: list[str] = []

    for line in unfolded:
        clean = line.strip()
        if clean == "BEGIN:VEVENT":
            inside_vevent = True
            current_vevent_lines = []
        elif clean == "END:VEVENT":
            if inside_vevent:
                ev_list = parse_vevent_block(current_vevent_lines, horizon_days=horizon_days)
                events.extend(ev_list)
                inside_vevent = False
                current_vevent_lines = []
        elif inside_vevent:
            current_vevent_lines.append(line)

    return events


def parse_ics_file(file_path: Path | str, horizon_days: int = 60) -> list[ParsedCalendarEvent]:
    """Read and parse an iCalendar (.ics) file from disk."""
    p = Path(file_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Calendar file not found: {p}")
    try:
        content = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = p.read_text(encoding="latin-1")
    return parse_ics_content(content, horizon_days=horizon_days)


def scan_vault_events_by_uid(
    vault_path: Path,
    folder: str = "Calendar",
) -> dict[str, tuple[Path, dict]]:
    """Scan existing Calendar notes in the vault and map UID -> (file_path, frontmatter)."""
    cal_dir = vault_path / folder.strip("/")
    if not cal_dir.exists():
        return {}

    uid_map: dict[str, tuple[Path, dict]] = {}
    for md_file in cal_dir.glob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = _parse_calendar_frontmatter(text)
        ev_uid = meta.get("uid")
        if ev_uid:
            uid_map[str(ev_uid).strip()] = (md_file, meta)

    return uid_map


async def _write_note_with_fallback(
    obsidian: ObsidianRestClient,
    vault_root: Path,
    rel_path: str,
    content: str,
) -> None:
    """Write note via Obsidian REST API, falling back to direct vault filesystem write if API is offline."""
    try:
        await obsidian.create_or_overwrite_note(rel_path, content)
    except Exception as exc:
        local_p = (vault_root / rel_path).resolve()
        local_p.parent.mkdir(parents=True, exist_ok=True)
        local_p.write_text(content, encoding="utf-8")


async def _delete_note_with_fallback(
    obsidian: ObsidianRestClient,
    vault_root: Path,
    rel_path: str,
) -> None:
    """Delete note via Obsidian REST API, falling back to direct vault filesystem unlink if API is offline."""
    try:
        await obsidian.delete_note(rel_path)
    except Exception:
        local_p = (vault_root / rel_path).resolve()
        if local_p.exists():
            local_p.unlink()


async def import_ics_file_to_vault(
    obsidian: ObsidianRestClient,
    vault_root: Path,
    ics_path: Path | str,
    calendar_folder: str = "Calendar",
    horizon_days: int = 60,
    notify: bool = True,
) -> CalendarImportResult:
    """Import an iCalendar (.ics) file into the Obsidian vault Full Calendar directory with deduplication."""
    path_obj = Path(ics_path).resolve()
    parsed_events = parse_ics_file(path_obj, horizon_days=horizon_days)
    result = CalendarImportResult(total_events=len(parsed_events))

    if not parsed_events:
        return result

    # Index existing events by UID
    existing_by_uid = scan_vault_events_by_uid(vault_root, folder=calendar_folder)

    for event in parsed_events:
        try:
            target_rel_path = event.safe_filename(folder=calendar_folder)
            markdown_content = event.to_markdown()

            if event.uid in existing_by_uid:
                existing_file, existing_meta = existing_by_uid[event.uid]
                existing_rel_path = str(existing_file.relative_to(vault_root))

                # Check if event moved or changed
                existing_content = existing_file.read_text(encoding="utf-8")
                if existing_rel_path == target_rel_path and existing_content.strip() == markdown_content.strip():
                    result.skipped += 1
                    continue

                # If filename / date changed, remove old note
                if existing_rel_path != target_rel_path:
                    await _delete_note_with_fallback(obsidian, vault_root, existing_rel_path)

                await _write_note_with_fallback(obsidian, vault_root, target_rel_path, markdown_content)
                result.updated += 1
                result.imported_files.append(target_rel_path)
                existing_by_uid[event.uid] = (vault_root / target_rel_path, _parse_calendar_frontmatter(markdown_content))
            else:
                await _write_note_with_fallback(obsidian, vault_root, target_rel_path, markdown_content)
                result.created += 1
                result.imported_files.append(target_rel_path)
                existing_by_uid[event.uid] = (vault_root / target_rel_path, _parse_calendar_frontmatter(markdown_content))
        except Exception as exc:
            err_msg = f"Failed to import '{event.title}': {exc}"
            logger.error(err_msg, exc_info=True)
            result.errors.append(err_msg)

    # Reset Full Calendar cache
    try:
        await obsidian.execute_command("obsidian-full-calendar:full-calendar-reset")
    except Exception as exc:
        logger.debug("Could not trigger full-calendar-reset: %s", exc)

    if notify and result.changed_count > 0:
        send_macos_notification(
            title="ISA Calendar Import",
            subtitle=f"{result.created} new, {result.updated} updated",
            message=f"Imported from {path_obj.name}",
        )

    return result
