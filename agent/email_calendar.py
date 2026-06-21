"""Deadline extraction, calendar actions, and conflict checks for inbox triage."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from openai import AzureOpenAI

from agent.conflict_checker import check_deadline_proximity, check_session_conflicts
from agent.graph import create_calendar_event, get_calendar_events

load_dotenv()
logger = logging.getLogger(__name__)

HK_TZ = ZoneInfo("Asia/Hong_Kong")

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

_DEADLINE_LINE = re.compile(
    r"(?:submission\s+)?deadline\s*:?\s*"
    r"(?:(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s+)?"
    r"(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(\d{1,2}),?\s+(\d{4})\s+at\s+(\d{1,2}):(\d{2})\s*(am|pm)",
    re.IGNORECASE,
)

_AT_DATETIME = re.compile(
    r"(?:on\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s+"
    r"(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(\d{1,2}),?\s+(\d{4})\s+at\s+(\d{1,2}):(\d{2})\s*(am|pm)",
    re.IGNORECASE,
)

_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

_TALK_SESSION = re.compile(
    r"(?:talk|seminar|workshop|lecture|briefing|info session|sharing session|oral examination)"
    r".{0,80}?"
    r"(?:on\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s+"
    r"(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(\d{1,2}),?\s+(\d{4})\s+at\s+(\d{1,2}):(\d{2})\s*(am|pm)",
    re.IGNORECASE,
)

_DATE_TIME_LINE = re.compile(
    r"date\s*/\s*time\s*:\s*"
    r"(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(\d{1,2})\s+"
    r"(\d{4})"
    r"(?:\s*\([a-z]{3,9}\))?"
    r"\s+"
    r"(\d{1,2}):(\d{2})\s*(am|pm)"
    r"\s*[–\-—]\s*"
    r"(\d{1,2}):(\d{2})\s*(am|pm)",
    re.IGNORECASE,
)

openai_client = None
if os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_API_KEY"):
    openai_client = AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version="2024-12-01-preview",
    )
DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")


def _to_24h(hour: int, minute: int, meridiem: str) -> tuple[int, int]:
    meridiem = meridiem.lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    return hour, minute


def _build_deadline(month_name: str, day: int, year: int, hour: int, minute: int, meridiem: str) -> dict | None:
    month = _MONTHS.get(month_name.lower())
    if not month:
        return None
    try:
        hour, minute = _to_24h(hour, minute, meridiem)
        dt = datetime(year, month, day, hour, minute, tzinfo=HK_TZ)
    except ValueError:
        return None
    end_iso = dt.strftime("%Y-%m-%dT%H:%M:%S")
    start_dt = dt - timedelta(minutes=30)
    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
    return {
        "deadline_iso": end_iso,
        "start_iso": start_iso,
        "end_iso": end_iso,
        "deadline_display": dt.strftime("%A, %d %B %Y at %I:%M %p HKT"),
        "deadline_date": dt.date().isoformat(),
        "kind": "deadline",
    }


def _build_event_range(
    month_name: str,
    day: int,
    year: int,
    start_hour: int,
    start_minute: int,
    start_meridiem: str,
    end_hour: int,
    end_minute: int,
    end_meridiem: str,
) -> dict | None:
    month = _MONTHS.get(month_name.lower())
    if not month:
        return None
    try:
        start_hour, start_minute = _to_24h(start_hour, start_minute, start_meridiem)
        end_hour, end_minute = _to_24h(end_hour, end_minute, end_meridiem)
        start_dt = datetime(year, month, day, start_hour, start_minute, tzinfo=HK_TZ)
        end_dt = datetime(year, month, day, end_hour, end_minute, tzinfo=HK_TZ)
        if end_dt <= start_dt:
            end_dt += timedelta(hours=1)
    except ValueError:
        return None
    display = (
        f"{start_dt.strftime('%B %d %Y (%a) %I:%M %p')} – "
        f"{end_dt.strftime('%I:%M %p')} HKT"
    )
    return {
        "start_iso": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "end_iso": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "deadline_display": display,
        "deadline_date": start_dt.date().isoformat(),
        "kind": "event",
    }


def extract_deadline(subject: str, preview: str) -> dict | None:
    text = f"{subject or ''}\n{preview or ''}"
    for pattern in (_DEADLINE_LINE, _AT_DATETIME):
        match = pattern.search(text)
        if match:
            groups = match.groups()
            return _build_deadline(groups[0], int(groups[1]), int(groups[2]), int(groups[3]), int(groups[4]), groups[5])
    iso = _ISO_DATE.search(text)
    if iso and "deadline" in text.lower():
        try:
            dt = datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)), 23, 59, tzinfo=HK_TZ)
        except ValueError:
            return None
        return {
            "deadline_iso": dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "start_iso": dt.replace(hour=23, minute=29).strftime("%Y-%m-%dT%H:%M:%S"),
            "end_iso": dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "deadline_display": dt.strftime("%A, %d %B %Y at 11:59 PM HKT"),
            "deadline_date": dt.date().isoformat(),
            "kind": "deadline",
        }
    return None


def extract_event_session(subject: str, preview: str) -> dict | None:
    text = f"{subject or ''}\n{preview or ''}"
    match = _DATE_TIME_LINE.search(text)
    if match:
        return _build_event_range(
            match.group(1),
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
            int(match.group(5)),
            match.group(6),
            int(match.group(7)),
            int(match.group(8)),
            match.group(9),
        )
    match = _TALK_SESSION.search(text)
    if not match:
        return None
    built = _build_deadline(match.group(1), int(match.group(2)), int(match.group(3)), int(match.group(4)), int(match.group(5)), match.group(6))
    if not built:
        return None
    end_dt = datetime.fromisoformat(built["end_iso"]).replace(tzinfo=HK_TZ)
    start_dt = end_dt - timedelta(hours=1)
    built["start_iso"] = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
    built["kind"] = "event"
    return built


def _calendar_key(subject: str, start_iso: str) -> str:
    return f"{(subject or '').strip().lower()}|{start_iso}"


def _already_added(profile: dict, key: str) -> bool:
    stored = profile.get("inbox_calendar_added") or []
    if not isinstance(stored, list):
        return False
    return key in stored


def _mark_added(profile: dict, key: str) -> None:
    stored = list(profile.get("inbox_calendar_added") or [])
    if key not in stored:
        stored.append(key)
    profile["inbox_calendar_added"] = stored[-100:]


def _normalize_action_step(action: str) -> str:
    text = str(action or "").strip()
    text = re.sub(r"^step\s+\d+\s*[:.\-)\]]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\d+\s*[:.\-)\]]\s*", "", text)
    return text.strip()


def _heuristic_actions(subject: str, preview: str, timing: dict | None) -> list[str]:
    text = f"{subject} {preview}".lower()
    actions = []
    if timing and timing.get("kind") == "deadline":
        actions.append(f"Submit or complete the required action before {timing.get('deadline_display', 'the deadline')}.")
    if "register" in text or "registration" in text or "sign up" in text:
        actions.append("Register or sign up if you plan to participate.")
    if "submit" in text or "submission" in text:
        actions.append("Prepare and submit your materials before the deadline.")
    if "attend" in text or "join" in text or "seminar" in text or "workshop" in text:
        actions.append("Block time to attend if you are interested.")
    if not actions:
        actions.append("Review this email and complete any required action before the date mentioned.")
    return [_normalize_action_step(action) for action in actions[:3]]


def suggest_urgent_actions(subject: str, preview: str, timing: dict | None) -> list[str]:
    heuristic = _heuristic_actions(subject, preview, timing)
    if not openai_client:
        return heuristic
    prompt = f"""
You help HKU students act on urgent emails. Return JSON only:
{{"actions": ["step 1", "step 2"]}}

Rules:
- 2-3 short, concrete steps
- Mention the deadline if one exists
- Do not invent requirements not implied by the email
- Do not prefix steps with "Step 1" or numbers — plain sentences only

Subject: {subject}
Preview: {preview}
Deadline: {(timing or {}).get('deadline_display') or 'unknown'}
"""
    try:
        response = openai_client.chat.completions.create(
            model=DEPLOYMENT,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=200,
            temperature=0.2,
        )
        parsed = json.loads(response.choices[0].message.content or "{}")
        actions = parsed.get("actions") or []
        cleaned = [_normalize_action_step(str(item)) for item in actions if str(item).strip()]
        cleaned = [item for item in cleaned if item]
        return cleaned[:3] or heuristic
    except Exception as exc:
        logger.warning("Urgent action LLM failed: %s", exc)
        return heuristic


def _weekday_name(dt: datetime) -> str:
    return dt.strftime("%A")


def _check_timetable_conflicts(timing: dict, profile: dict) -> list[dict]:
    if not timing:
        return []
    try:
        start_dt = datetime.fromisoformat(timing["start_iso"]).replace(tzinfo=HK_TZ)
        end_dt = datetime.fromisoformat(timing["end_iso"]).replace(tzinfo=HK_TZ)
    except (KeyError, ValueError):
        return []
    session = [{
        "day": _weekday_name(start_dt),
        "start": start_dt.strftime("%H:%M"),
        "end": end_dt.strftime("%H:%M"),
        "label": "Email event/deadline",
    }]
    blocked = profile.get("timetable", {}).get("blocked_slots", [])
    return check_session_conflicts(session, blocked)


def _check_outlook_conflicts(user_token: str, timing: dict) -> list[str]:
    if not user_token or not timing:
        return []
    try:
        start_dt = datetime.fromisoformat(timing["start_iso"]).replace(tzinfo=HK_TZ)
        end_dt = datetime.fromisoformat(timing["end_iso"]).replace(tzinfo=HK_TZ)
    except (KeyError, ValueError):
        return []
    window_start = (start_dt - timedelta(hours=1)).isoformat()
    window_end = (end_dt + timedelta(hours=1)).isoformat()
    result = get_calendar_events(user_token, window_start, window_end)
    if not result.get("success"):
        return []
    notes = []
    for event in result.get("events") or []:
        subject = event.get("subject") or "Busy"
        start = ((event.get("start") or {}).get("dateTime") or "")[:16]
        if start:
            notes.append(f"Overlaps with calendar event **{subject}** at {start}.")
    return notes[:3]


def _compose_calendar_note(timing: dict, profile: dict, user_token: str, *, include_outlook: bool) -> tuple[list[dict], str]:
    flags = _check_timetable_conflicts(timing, profile)
    notes = [flag.get("message", "") for flag in flags if flag.get("message")]
    if include_outlook:
        notes.extend(_check_outlook_conflicts(user_token, timing))
    proximity = check_deadline_proximity(
        timing.get("deadline_date") if timing else None,
        profile.get("timetable", {}).get("upcoming_deadlines", []),
    )
    if proximity and proximity.get("message"):
        notes.append(proximity["message"])
    calendar_note = " ".join(note for note in notes if note).strip()
    return flags, calendar_note


def _auto_add_calendar(user_token: str, profile: dict, subject: str, timing: dict) -> dict:
    result = {"calendar_added": False, "calendar_error": ""}
    if not user_token or not timing:
        return result
    key = _calendar_key(subject, timing["start_iso"])
    if _already_added(profile, key):
        result["calendar_added"] = True
        result["calendar_skipped"] = True
        return result
    title = f"Deadline: {subject[:80]}" if timing.get("kind") == "deadline" else f"Event: {subject[:80]}"
    created = create_calendar_event(
        user_token,
        title,
        timing["start_iso"],
        timing["end_iso"],
    )
    if created.get("success"):
        _mark_added(profile, key)
        result["calendar_added"] = True
        result["calendar_event_id"] = created.get("event_id", "")
        result["calendar_title"] = title
    else:
        result["calendar_error"] = created.get("error") or "Calendar create failed"
        logger.error("Auto calendar add failed for urgent email: %s", result["calendar_error"])
    return result


def enrich_inbox_with_calendar(
    urgent_items: list[dict],
    relevant_items: list[dict],
    profile: dict,
    user_token: str | None,
) -> None:
    """Add action steps, calendar events, and conflict notes to inbox items."""
    for item in urgent_items:
        subject = item.get("subject") or ""
        preview = item.get("body_preview") or ""
        timing = extract_deadline(subject, preview) or extract_event_session(subject, preview)
        item["timing"] = timing
        item["action_items"] = suggest_urgent_actions(subject, preview, timing)
        conflict_flags, calendar_note = _compose_calendar_note(
            timing, profile, user_token or "", include_outlook=False,
        )
        item["conflict_flags"] = conflict_flags
        if calendar_note:
            item["calendar_note"] = calendar_note
        if timing and user_token:
            item.update(_auto_add_calendar(user_token, profile, subject, timing))

    for item in relevant_items:
        subject = item.get("subject") or ""
        preview = item.get("body_preview") or ""
        timing = extract_event_session(subject, preview) or extract_deadline(subject, preview)
        item["timing"] = timing
        item["action_items"] = _heuristic_actions(subject, preview, timing)
        conflict_flags, calendar_note = _compose_calendar_note(
            timing, profile, user_token or "", include_outlook=bool(user_token),
        )
        item["conflict_flags"] = conflict_flags
        if calendar_note:
            item["calendar_note"] = calendar_note
        item["calendar_added"] = False
        item["needs_registration"] = bool(timing)


def mark_email_calendar_added(profile: dict, subject: str, start_iso: str) -> None:
    _mark_added(profile, _calendar_key(subject, start_iso))
