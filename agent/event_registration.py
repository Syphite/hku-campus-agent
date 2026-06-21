"""Calendar-aware event registration: conflict detection and replacement planning."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent.conflict_checker import check_deadline_proximity
from agent.graph import delete_calendar_event, get_calendar_events

logger = logging.getLogger(__name__)

HK_TZ = ZoneInfo("Asia/Hong_Kong")

_DEADLINE_HINTS = (
    "deadline", "submission", "submit", "due", "competition",
    "writing", "essay", "assignment", "截止", "提交", "截止日",
)


def _parse_local_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        normalized = str(value).split(".")[0]
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=HK_TZ)
        return dt.astimezone(HK_TZ)
    except (TypeError, ValueError):
        return None


def _format_time_range(start_iso: str, end_iso: str) -> str:
    start_dt = _parse_local_dt(start_iso)
    end_dt = _parse_local_dt(end_iso)
    if not start_dt or not end_dt:
        return ""
    if start_dt.date() == end_dt.date():
        return (
            f"{start_dt.strftime('%a %d %b %Y %I:%M %p')} – "
            f"{end_dt.strftime('%I:%M %p')} HKT"
        )
    return (
        f"{start_dt.strftime('%a %d %b %I:%M %p')} – "
        f"{end_dt.strftime('%a %d %b %I:%M %p')} HKT"
    )


_MULTI_DAY_CN_RANGE = re.compile(
    r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})"
    r".{0,12}?"
    r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})",
)


def _infer_year_from_text(text: str, default: int = 2026) -> int:
    match = re.search(r"(20\d{2})", text or "")
    if match:
        return int(match.group(1))
    return default


def _parse_cn_datetime_range(text: str) -> tuple[str, str] | None:
    match = _MULTI_DAY_CN_RANGE.search(text or "")
    if not match:
        return None
    year = _infer_year_from_text(text)
    start_month, start_day, start_hour, start_minute, end_month, end_day, end_hour, end_minute = match.groups()
    try:
        start_dt = datetime(
            year, int(start_month), int(start_day), int(start_hour), int(start_minute), tzinfo=HK_TZ,
        )
        end_dt = datetime(
            year, int(end_month), int(end_day), int(end_hour), int(end_minute), tzinfo=HK_TZ,
        )
    except ValueError:
        return None
    return (
        start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
    )


def normalize_event_calendar_fields(event: dict) -> dict:
    """Attach calendar_start_iso / calendar_end_iso from structured session data."""
    if event.get("calendar_start_iso") and event.get("calendar_end_iso"):
        event["deadline_display"] = event.get("deadline_display") or _format_time_range(
            event["calendar_start_iso"], event["calendar_end_iso"]
        )
        return event

    sessions = event.get("event_sessions") or []
    dated = [session for session in sessions if isinstance(session, dict) and session.get("date")]
    if dated:
        session = dated[0]
        start_date = str(session.get("date"))[:10]
        end_date = str(session.get("end_date") or session.get("date"))[:10]
        start_time = str(session.get("start") or "09:00")[:5]
        end_time = str(session.get("end") or "10:00")[:5]
        if len(start_time) == 4:
            start_time = f"0{start_time}"
        if len(end_time) == 4:
            end_time = f"0{end_time}"

        event["calendar_start_iso"] = f"{start_date}T{start_time}:00"
        event["calendar_end_iso"] = f"{end_date}T{end_time}:00"
        event["deadline_display"] = _format_time_range(
            event["calendar_start_iso"], event["calendar_end_iso"]
        )
        return event

    source_text = " ".join(
        str(event.get(key) or "")
        for key in ("title", "summary", "eligibility", "_source_text")
    )
    parsed = _parse_cn_datetime_range(source_text)
    if parsed:
        event["calendar_start_iso"], event["calendar_end_iso"] = parsed
        event["deadline_display"] = _format_time_range(parsed[0], parsed[1])
    return event


def resolve_event_schedule(event: dict) -> dict | None:
    """Return start/end ISO datetimes and display strings for calendar registration."""
    event = normalize_event_calendar_fields(dict(event or {}))
    start_iso = event.get("calendar_start_iso")
    end_iso = event.get("calendar_end_iso")
    if start_iso and end_iso:
        start_dt = _parse_local_dt(start_iso)
        end_dt = _parse_local_dt(end_iso)
        if start_dt and end_dt:
            return {
                "start_iso": start_iso,
                "end_iso": end_iso,
                "display_date": start_dt.strftime("%A, %d %B %Y"),
                "display_time": _format_time_range(start_iso, end_iso),
            }

    deadline = event.get("deadline")
    if deadline:
        try:
            event_date = datetime.strptime(str(deadline)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
        return {
            "start_iso": f"{event_date.isoformat()}T09:00:00",
            "end_iso": f"{event_date.isoformat()}T10:00:00",
            "display_date": event_date.strftime("%A, %d %B %Y"),
            "display_time": "09:00–10:00",
        }
    return None


def _looks_like_deadline_event(subject: str) -> bool:
    text = str(subject or "").lower()
    return any(hint in text for hint in _DEADLINE_HINTS)


def _graph_event_window(event: dict) -> tuple[str, str] | None:
    start_info = event.get("start") or {}
    end_info = event.get("end") or {}
    start_dt = _parse_local_dt(start_info.get("dateTime", ""))
    end_dt = _parse_local_dt(end_info.get("dateTime", ""))
    if not start_dt or not end_dt:
        return None
    return start_dt.isoformat(), end_dt.isoformat()


def _events_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    a0 = _parse_local_dt(start_a)
    a1 = _parse_local_dt(end_a)
    b0 = _parse_local_dt(start_b)
    b1 = _parse_local_dt(end_b)
    if not all([a0, a1, b0, b1]):
        return False
    return a0 < b1 and b0 < a1


def assess_registration_impact(
    user_token: str | None,
    profile: dict,
    *,
    title: str,
    start_iso: str,
    end_iso: str,
) -> dict:
    """
    Inspect Outlook calendar + profile deadlines for registration conflicts.
    Returns replaceable overlaps and informational deadline notes.
    """
    impact = {
        "warnings": [],
        "replace_events": [],
        "keep_events": [],
        "replace_event_ids": [],
    }

    proximity = check_deadline_proximity(
        str(end_iso)[:10],
        (profile.get("timetable") or {}).get("upcoming_deadlines") or [],
    )
    if proximity and proximity.get("message"):
        impact["warnings"].append(proximity["message"])

    if not user_token:
        return impact

    start_dt = _parse_local_dt(start_iso)
    end_dt = _parse_local_dt(end_iso)
    if not start_dt or not end_dt:
        return impact

    window_start = (start_dt - timedelta(hours=2)).isoformat()
    window_end = (end_dt + timedelta(hours=6)).isoformat()
    result = get_calendar_events(user_token, window_start, window_end)
    if not result.get("success"):
        return impact

    for cal_event in result.get("events") or []:
        if cal_event.get("isAllDay"):
            continue
        subject = str(cal_event.get("subject") or "Calendar event").strip()
        if not subject:
            continue
        window = _graph_event_window(cal_event)
        if not window:
            continue
        cal_start, cal_end = window
        if not _events_overlap(start_iso, end_iso, cal_start, cal_end):
            if _looks_like_deadline_event(subject):
                cal_start_dt = _parse_local_dt(cal_start)
                if cal_start_dt and start_dt.date() <= cal_start_dt.date() <= end_dt.date():
                    impact["keep_events"].append({
                        "event_id": cal_event.get("id", ""),
                        "subject": subject,
                        "start_iso": cal_start,
                        "end_iso": cal_end,
                    })
                    impact["warnings"].append(
                        f"You also have **{subject}** on "
                        f"{cal_start_dt.strftime('%d/%m at %I:%M %p')} — this will stay on your calendar."
                    )
            continue

        entry = {
            "event_id": cal_event.get("id", ""),
            "subject": subject,
            "start_iso": cal_start,
            "end_iso": cal_end,
        }
        if _looks_like_deadline_event(subject):
            impact["keep_events"].append(entry)
            cal_start_dt = _parse_local_dt(cal_start)
            when = cal_start_dt.strftime("%d/%m at %I:%M %p") if cal_start_dt else "that time"
            impact["warnings"].append(
                f"**{subject}** on {when} overlaps in time but looks like a deadline — I'll leave it unless you ask to change it later."
            )
            continue

        impact["replace_events"].append(entry)
        if entry["event_id"]:
            impact["replace_event_ids"].append(entry["event_id"])
        cal_start_dt = _parse_local_dt(cal_start)
        when = cal_start_dt.strftime("%d/%m at %I:%M %p") if cal_start_dt else "that time"
        impact["warnings"].append(
            f"**{subject}** on {when} conflicts with this event and would be removed if you confirm."
        )

    return impact


def apply_registration_calendar_changes(
    user_token: str,
    *,
    title: str,
    start_iso: str,
    end_iso: str,
    location: str = "",
    replace_event_ids: list[str] | None = None,
) -> dict:
    removed = []
    errors = []
    for event_id in replace_event_ids or []:
        if not event_id:
            continue
        result = delete_calendar_event(user_token, event_id)
        if result.get("success"):
            removed.append(event_id)
        else:
            errors.append(result.get("error") or f"Could not remove event {event_id}")

    from agent.graph import create_calendar_event

    created = create_calendar_event(user_token, title, start_iso, end_iso, location)
    return {
        "created": created,
        "removed_ids": removed,
        "errors": errors,
    }
