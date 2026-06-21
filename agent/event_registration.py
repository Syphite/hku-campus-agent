"""Calendar-aware event registration: conflict detection and replacement planning."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from openai import AzureOpenAI

from agent.conflict_checker import check_deadline_proximity
from agent.datetime_utils import HK_TZ, parse_graph_datetime_field
from agent.graph import delete_calendar_event, get_calendar_events

load_dotenv()
logger = logging.getLogger(__name__)

PLANNING_LOOKAHEAD_DAYS = 7
PROXIMITY_HOURS = 2

_DEADLINE_HINTS = (
    "deadline", "submission", "submit", "due", "competition",
    "writing", "essay", "assignment", "截止", "提交", "截止日",
)

openai_client = None
if os.environ.get("AZURE_OPENAI_ENDPOINT") and os.environ.get("AZURE_OPENAI_API_KEY"):
    openai_client = AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version="2024-12-01-preview",
    )
DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")


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


def _format_hkt_moment(dt: datetime) -> str:
    return dt.strftime("%d/%m at %I:%M %p")


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


def _graph_event_window(event: dict) -> tuple[datetime, datetime] | None:
    start_dt = parse_graph_datetime_field(event.get("start") or {})
    end_dt = parse_graph_datetime_field(event.get("end") or {})
    if not start_dt or not end_dt:
        return None
    return start_dt, end_dt


def _events_overlap_dt(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


def _events_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    a0 = _parse_local_dt(start_a)
    a1 = _parse_local_dt(end_a)
    b0 = _parse_local_dt(start_b)
    b1 = _parse_local_dt(end_b)
    if not all([a0, a1, b0, b1]):
        return False
    return _events_overlap_dt(a0, a1, b0, b1)


def _is_relevant_calendar_item(cal_start: datetime, cal_end: datetime, reg_start: datetime, reg_end: datetime) -> bool:
    planning_start = reg_start - timedelta(days=PLANNING_LOOKAHEAD_DAYS)
    return cal_end >= planning_start and cal_start <= reg_end + timedelta(days=1)


def _build_calendar_fact(
    cal_event: dict,
    reg_start: datetime,
    reg_end: datetime,
) -> dict | None:
    window = _graph_event_window(cal_event)
    if not window:
        return None
    cal_start, cal_end = window
    if not _is_relevant_calendar_item(cal_start, cal_end, reg_start, reg_end):
        return None

    subject = str(cal_event.get("subject") or "Calendar event").strip()
    deadline_hint = _looks_like_deadline_event(subject)
    overlaps = _events_overlap_dt(cal_start, cal_end, reg_start, reg_end)

    hours_before_start = None
    if cal_end <= reg_start:
        hours_before_start = round((reg_start - cal_end).total_seconds() / 3600, 1)

    days_before_start = None
    if cal_end.date() <= reg_start.date():
        days_before_start = (reg_start.date() - cal_end.date()).days

    return {
        "event_id": cal_event.get("id", ""),
        "subject": subject,
        "start_hkt": cal_start.strftime("%Y-%m-%dT%H:%M:%S"),
        "end_hkt": cal_end.strftime("%Y-%m-%dT%H:%M:%S"),
        "display": f"{_format_hkt_moment(cal_start)} – {cal_end.strftime('%I:%M %p')} HKT",
        "deadline_hint": deadline_hint,
        "overlaps_registration": overlaps,
        "hours_before_registration_start": hours_before_start,
        "days_before_registration_start": days_before_start,
        "same_day_as_registration_start": cal_start.date() == reg_start.date(),
        "same_day_as_registration_end": cal_end.date() == reg_end.date(),
    }


def _profile_deadline_facts(profile: dict, reg_start: datetime, reg_end: datetime) -> list[dict]:
    facts = []
    today = datetime.now(HK_TZ).date()
    planning_start = reg_start.date() - timedelta(days=PLANNING_LOOKAHEAD_DAYS)
    for item in (profile.get("timetable") or {}).get("upcoming_deadlines") or []:
        date_str = str(item.get("date") or "")[:10]
        if not date_str:
            continue
        try:
            dl_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if dl_date < today or dl_date > reg_end.date():
            continue
        if dl_date < planning_start:
            continue
        facts.append({
            "label": str(item.get("label") or "Deadline"),
            "date": date_str,
            "days_before_registration_start": (reg_start.date() - dl_date).days,
            "same_day_as_registration_start": dl_date == reg_start.date(),
            "same_day_as_registration_end": dl_date == reg_end.date(),
        })
    return facts


def _rule_based_calendar_decisions(facts: list[dict]) -> dict:
    """Decide which overlapping events to replace; keep deadline entries."""
    replace_events = []
    keep_events = []
    replace_event_ids = []
    hard_conflict_warnings = []

    for fact in facts:
        if not fact.get("overlaps_registration"):
            continue
        entry = {
            "event_id": fact.get("event_id", ""),
            "subject": fact["subject"],
            "start_iso": fact["start_hkt"],
            "end_iso": fact["end_hkt"],
        }
        if fact.get("deadline_hint"):
            keep_events.append(entry)
            continue

        replace_events.append(entry)
        if entry["event_id"]:
            replace_event_ids.append(entry["event_id"])
        hard_conflict_warnings.append(
            f"**{fact['subject']}** ({fact['display']}) conflicts with this event and would be removed if you confirm."
        )

    return {
        "replace_events": replace_events,
        "keep_events": keep_events,
        "replace_event_ids": replace_event_ids,
        "hard_conflict_warnings": hard_conflict_warnings,
    }


def _fallback_registration_warnings(
    facts: list[dict],
    profile_deadlines: list[dict],
    *,
    title: str,
    reg_start: datetime,
    reg_end: datetime,
) -> list[str]:
    warnings = []
    seen_subjects = set()

    for fact in facts:
        subject = fact["subject"]
        if subject in seen_subjects:
            continue

        if fact.get("overlaps_registration") and fact.get("deadline_hint"):
            seen_subjects.add(subject)
            warnings.append(
                f"**{subject}** is due {fact['display']} — overlaps this event on your calendar; I'll leave it unless you ask to change it."
            )
            continue

        hours_before = fact.get("hours_before_registration_start")
        if hours_before is not None and 0 <= hours_before <= PROXIMITY_HOURS:
            seen_subjects.add(subject)
            warnings.append(
                f"**{subject}** ends about {hours_before:g} hour(s) before this starts ({fact['display']}) — double-check you're free in time."
            )
            continue

        days_before = fact.get("days_before_registration_start")
        if fact.get("deadline_hint") and days_before is not None and 1 <= days_before <= PLANNING_LOOKAHEAD_DAYS:
            seen_subjects.add(subject)
            warnings.append(
                f"**{subject}** ({fact['display']}) is {days_before} day(s) before this event — plan ahead so the deadline doesn't clash with prep or travel."
            )
            continue

        if (
            fact.get("deadline_hint")
            and (fact.get("same_day_as_registration_start") or fact.get("same_day_as_registration_end"))
            and not fact.get("overlaps_registration")
        ):
            seen_subjects.add(subject)
            warnings.append(
                f"**{subject}** on the same day ({fact['display']}) — no time clash, but worth keeping in mind."
            )

    for item in profile_deadlines:
        label = item["label"]
        if label in seen_subjects:
            continue
        days_before = item.get("days_before_registration_start")
        if days_before is not None and 0 <= days_before <= PLANNING_LOOKAHEAD_DAYS:
            seen_subjects.add(label)
            when = item["date"]
            if days_before == 0:
                warnings.append(
                    f"Your **{label}** is due on the same day as this event starts ({when}) — allow time for both."
                )
            else:
                warnings.append(
                    f"Your **{label}** ({when}) is {days_before} day(s) before this event — plan submission time early."
                )

    return warnings[:4]


def _llm_registration_warnings(
    facts: list[dict],
    profile_deadlines: list[dict],
    *,
    title: str,
    reg_start: datetime,
    reg_end: datetime,
    hard_conflict_warnings: list[str],
) -> list[str]:
    if not openai_client or (not facts and not profile_deadlines):
        return []

    prompt = f"""
You help HKU students review calendar impact before registering for an event.
Return JSON only: {{"warnings": ["...", "..."]}}

Registration:
- title: {title}
- start_hkt: {reg_start.strftime("%Y-%m-%d %H:%M")}
- end_hkt: {reg_end.strftime("%Y-%m-%d %H:%M")}

Calendar items (all times are HKT, pre-computed — use exactly as given):
{json.dumps(facts, ensure_ascii=False)}

Profile deadlines:
{json.dumps(profile_deadlines, ensure_ascii=False)}

Rules:
- Use ONLY the times and facts provided; never invent or guess times
- Do NOT claim a time overlap unless overlaps_registration is true
- Flag deadlines on the same day or within {PLANNING_LOOKAHEAD_DAYS} days before the event as needing advance planning
- Flag items ending 0–{PROXIMITY_HOURS} hours before registration start as worth double-checking availability
- Hard remove conflicts are listed separately — do not repeat them unless adding context
- Bold event names with **name**
- Max 4 short, student-friendly warnings
- If nothing meaningful to flag beyond hard conflicts, return an empty warnings list

Hard conflicts already handled:
{json.dumps(hard_conflict_warnings, ensure_ascii=False)}
"""
    try:
        response = openai_client.chat.completions.create(
            model=DEPLOYMENT,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=350,
            temperature=0.2,
        )
        parsed = json.loads(response.choices[0].message.content or "{}")
        warnings = [str(item).strip() for item in (parsed.get("warnings") or []) if str(item).strip()]
        return warnings[:4]
    except Exception as exc:
        logger.warning("Registration conflict LLM failed: %s", exc)
        return []


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
    Returns replaceable overlaps and informational planning notes.
    """
    impact = {
        "warnings": [],
        "replace_events": [],
        "keep_events": [],
        "replace_event_ids": [],
    }

    start_dt = _parse_local_dt(start_iso)
    end_dt = _parse_local_dt(end_iso)

    for ref_iso in (start_iso, end_iso):
        proximity = check_deadline_proximity(
            str(ref_iso)[:10],
            (profile.get("timetable") or {}).get("upcoming_deadlines") or [],
        )
        if proximity and proximity.get("message"):
            msg = proximity["message"]
            if msg not in impact["warnings"]:
                impact["warnings"].append(msg)

    if not user_token or not start_dt or not end_dt:
        return impact

    window_start = (start_dt - timedelta(days=PLANNING_LOOKAHEAD_DAYS)).isoformat()
    window_end = (end_dt + timedelta(days=1)).isoformat()
    result = get_calendar_events(user_token, window_start, window_end)
    if not result.get("success"):
        return impact

    calendar_facts = []
    for cal_event in result.get("events") or []:
        if cal_event.get("isAllDay"):
            continue
        fact = _build_calendar_fact(cal_event, start_dt, end_dt)
        if fact:
            calendar_facts.append(fact)

    profile_deadlines = _profile_deadline_facts(profile, start_dt, end_dt)
    decisions = _rule_based_calendar_decisions(calendar_facts)
    impact["replace_events"] = decisions["replace_events"]
    impact["keep_events"] = decisions["keep_events"]
    impact["replace_event_ids"] = decisions["replace_event_ids"]

    soft_warnings = _llm_registration_warnings(
        calendar_facts,
        profile_deadlines,
        title=title,
        reg_start=start_dt,
        reg_end=end_dt,
        hard_conflict_warnings=decisions["hard_conflict_warnings"],
    )
    if not soft_warnings:
        soft_warnings = _fallback_registration_warnings(
            calendar_facts,
            profile_deadlines,
            title=title,
            reg_start=start_dt,
            reg_end=end_dt,
        )

    impact["warnings"].extend(decisions["hard_conflict_warnings"])
    for warning in soft_warnings:
        if warning not in impact["warnings"]:
            impact["warnings"].append(warning)

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
