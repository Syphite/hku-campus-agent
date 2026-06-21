"""Sync onboarding schedule slots to Outlook as recurring events (3-week window)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from agent.datetime_utils import combine_date_and_time, normalize_time_hhmm, validate_iso_datetime
from agent.graph import create_recurring_calendar_event

logger = logging.getLogger(__name__)

HK_TZ = timezone(timedelta(hours=8))
SCHEDULE_WEEKS = 3
_WEEKDAY_TO_GRAPH = {
    "monday": "monday",
    "tuesday": "tuesday",
    "wednesday": "wednesday",
    "thursday": "thursday",
    "friday": "friday",
    "saturday": "saturday",
    "sunday": "sunday",
}


def default_schedule_slots() -> list[dict]:
    """Demo fallback when onboarding leaves all class rows empty."""
    return [
        {"day": "Monday", "start": "10:00", "end": "12:00", "label": "Class / study block"},
        {"day": "Wednesday", "start": "10:00", "end": "12:00", "label": "Class / study block"},
        {"day": "Friday", "start": "10:00", "end": "12:00", "label": "Class / study block"},
    ]


def parse_onboarding_schedule_slots(form_data: dict) -> list[dict]:
    """Parse class1–class3 rows from onboarding form submission."""
    blocked_slots = []
    for i in range(1, 4):
        code = str(form_data.get(f"class{i}_code", "") or "").strip()
        day = str(form_data.get(f"class{i}_day", "") or "").strip()
        start = str(form_data.get(f"class{i}_start", "") or "").strip()
        end = str(form_data.get(f"class{i}_end", "") or "").strip()
        if code and day and start and end:
            blocked_slots.append({
                "day": day,
                "start": start,
                "end": end,
                "label": code,
            })
    return blocked_slots


def resolve_schedule_slots(form_data: dict | None, existing_slots: list | None = None) -> list[dict]:
    """Use submitted slots, existing profile slots, or defaults."""
    if existing_slots:
        return list(existing_slots)
    parsed = parse_onboarding_schedule_slots(form_data or {})
    if parsed:
        return parsed
    return default_schedule_slots()


def _schedule_range_dates() -> tuple[str, str]:
    now = datetime.now(HK_TZ)
    start = now.date()
    end = start + timedelta(days=SCHEDULE_WEEKS * 7 - 1)
    return start.isoformat(), end.isoformat()


def _next_occurrence(day_name: str, start_time: str) -> str:
    """ISO datetime for the next occurrence of weekday + start time (HK)."""
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    target = day_name.strip().lower()
    if target not in weekdays:
        return ""
    now = datetime.now(HK_TZ)
    target_idx = weekdays.index(target)
    days_ahead = (target_idx - now.weekday()) % 7
    if days_ahead == 0 and now.strftime("%H:%M") >= start_time:
        days_ahead = 7
    event_date = (now + timedelta(days=days_ahead)).date()
    time_part = normalize_time_hhmm(start_time) or "09:00"
    return combine_date_and_time(event_date.isoformat(), time_part)


def sync_schedule_to_calendar(profile: dict, user_token: str) -> dict:
    """
    Create recurring Outlook events for each blocked slot over the next 3 weeks.
    Updates profile timetable in-place; caller should save_profile.
    """
    if not user_token:
        return {"synced": 0, "errors": ["No Graph token"]}

    slots = (profile.get("timetable") or {}).get("blocked_slots") or []
    if not slots:
        slots = default_schedule_slots()
        profile.setdefault("timetable", {})["blocked_slots"] = slots

    timetable = profile.setdefault("timetable", {})
    existing_ids = list(timetable.get("calendar_event_ids") or [])
    if existing_ids:
        logger.info("Schedule already synced (%s events); skipping duplicate write", len(existing_ids))
        return {"synced": 0, "skipped": True, "errors": []}

    range_start, range_end = _schedule_range_dates()
    created_ids = []
    errors = []

    for slot in slots:
        day = slot.get("day", "")
        start = slot.get("start", "")
        end = slot.get("end", "")
        label = slot.get("label") or "Class"
        graph_day = _WEEKDAY_TO_GRAPH.get(str(day).strip().lower())
        start_hhmm = normalize_time_hhmm(start)
        end_hhmm = normalize_time_hhmm(end)
        if not graph_day or not start_hhmm or not end_hhmm:
            continue
        first_start = _next_occurrence(day, start_hhmm)
        if not first_start or not validate_iso_datetime(first_start):
            continue
        date_part = first_start.split("T")[0]
        first_end = combine_date_and_time(date_part, end_hhmm)
        if not validate_iso_datetime(first_end):
            continue
        result = create_recurring_calendar_event(
            user_token,
            title=label,
            day_of_week=graph_day,
            start_iso=first_start,
            end_iso=first_end,
            range_start=range_start,
            range_end=range_end,
        )
        if result.get("success"):
            event_id = result.get("event_id")
            if event_id:
                created_ids.append(event_id)
        else:
            errors.append(result.get("error") or "Unknown calendar error")

    timetable["calendar_event_ids"] = created_ids
    profile["pending_schedule_calendar_sync"] = False
    logger.info("Schedule calendar sync: %s events created", len(created_ids))
    return {"synced": len(created_ids), "errors": errors}
