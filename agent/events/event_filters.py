"""Filter events whose deadlines or scheduled end dates have passed."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

HK_TZ = ZoneInfo("Asia/Hong_Kong")


def _parse_iso_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _parse_iso_datetime_date(value) -> date | None:
    if not value:
        return None
    try:
        normalized = str(value).split(".")[0]
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=HK_TZ)
        return dt.astimezone(HK_TZ).date()
    except (TypeError, ValueError):
        return _parse_iso_date(value)


def event_cutoff_date(event: dict) -> date | None:
    """
    Date after which an event should no longer be shown.
    Prefer application deadline; fall back to scheduled end date.
    """
    deadline = _parse_iso_date(event.get("deadline"))
    if deadline:
        return deadline
    return _parse_iso_datetime_date(event.get("calendar_end_iso"))


def is_event_still_open(event: dict, *, today: date | None = None) -> bool:
    """True when the event deadline/end date is today or in the future."""
    today = today or date.today()
    cutoff = event_cutoff_date(event)
    if cutoff is None:
        return True
    return cutoff >= today


def filter_open_events(events: list, *, today: date | None = None) -> list:
    """Drop events whose deadline or scheduled end date has passed."""
    return [
        event for event in (events or [])
        if isinstance(event, dict) and is_event_still_open(event, today=today)
    ]
