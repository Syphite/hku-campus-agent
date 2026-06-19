"""
agent/events/conflict_checker.py

Checks matched events against a student's timetable and upcoming deadlines.
No LLM involved — pure date and time logic.

Two checks:
1. Session conflict  — recurring event sessions clash with blocked timetable slots
2. Deadline proximity — event deadline falls within 3 days of a student deadline,
                        suggest a buffer "aim to submit by" date

Called after event matching, on matched events only.
"""

from datetime import datetime, date, timedelta
from typing import Optional
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session conflict check
# ---------------------------------------------------------------------------

def _times_overlap(
    a_start: str, a_end: str,
    b_start: str, b_end: str
) -> bool:
    """
    Check if two time ranges overlap.
    Times are strings in "HH:MM" format.
    """
    def to_minutes(t: str) -> int | None:
        try:
            h, m = t.split(":")
            return int(h) * 60 + int(m)
        except (ValueError, AttributeError):
            return None  # Return None if format is bad

    a0, a1 = to_minutes(a_start), to_minutes(a_end)
    b0, b1 = to_minutes(b_start), to_minutes(b_end)

    # If any time is invalid, assume no overlap to prevent crashing.
    if a0 is None or a1 is None or b0 is None or b1 is None:
        return False

    return a0 < b1 and b0 < a1


def check_session_conflicts(
    event_sessions: list,
    timetable_slots: list
) -> list:
    """
    Compare event recurring sessions against student blocked timetable slots.

    Args:
        event_sessions: from extracted event, e.g.
            [{"day": "Tuesday", "start": "14:00", "end": "17:00", "label": "Team sessions"}]
        timetable_slots: from student profile, e.g.
            [{"day": "Tuesday", "start": "14:00", "end": "17:00", "label": "COMP3230 Lab"}]

    Returns:
        List of conflict dicts (empty if no conflicts)
    """
    conflicts = []
    for session in event_sessions:
        s_day   = session.get("day", "").strip().lower()
        s_start = session.get("start", "")
        s_end   = session.get("end", "")
        s_label = session.get("label", "event session")

        if not s_day or not s_start or not s_end:
            continue

        for slot in timetable_slots:
            t_day   = slot.get("day", "").strip().lower()
            t_start = slot.get("start", "")
            t_end   = slot.get("end", "")
            t_label = slot.get("label", "class")

            if t_day != s_day:
                continue
            if not t_start or not t_end:
                continue

            if _times_overlap(s_start, s_end, t_start, t_end):
                conflicts.append({
                    "type":          "session_conflict",
                    "day":           slot.get("day"),
                    "event_session": f"{s_label} ({s_start}–{s_end})",
                    "clashes_with":  f"{t_label} ({t_start}–{t_end})",
                    "message": (
                        f"Event session on {slot.get('day')} {s_start}–{s_end} "
                        f"clashes with {t_label} ({t_start}–{t_end}). "
                        f"Check if sessions are mandatory before registering."
                    )
                })

    return conflicts


# ---------------------------------------------------------------------------
# Deadline proximity check
# ---------------------------------------------------------------------------

def _parse_date(date_str: Optional[str]) -> Optional[date]:
    """Parse ISO date string to date object. Returns None if invalid."""
    if not date_str:
        return None
    try:
        return date.fromisoformat(date_str[:10])
    except ValueError:
        return None


def check_deadline_proximity(
    event_deadline_iso: Optional[str],
    upcoming_deadlines: list,
    buffer_days: int = 3,
    warning_window_days: int = 7
) -> Optional[dict]:
    """
    Check if event deadline falls dangerously close to a student's deadline.

    Args:
        event_deadline_iso: event application deadline as ISO string or None
        upcoming_deadlines: from student profile, e.g.
            [{"label": "COMP3230 Assignment 3", "date": "2026-06-15"}]
        buffer_days: how many days before the student deadline to suggest
                     submitting the event application (default 3)
        warning_window_days: flag if event deadline is within this many days
                             of a student deadline (default 7)

    Returns:
        Proximity warning dict or None if no concern
    """
    event_dl = _parse_date(event_deadline_iso)
    if not event_dl:
        return None

    today = date.today()
    if event_dl < today:
        return None  # already passed

    closest_conflict = None
    closest_gap = None

    for item in upcoming_deadlines:
        student_dl = _parse_date(item.get("date"))
        if not student_dl:
            continue
        if student_dl < today:
            continue

        # Days between event deadline and student deadline
        gap = abs((student_dl - event_dl).days)

        if gap <= warning_window_days:
            if closest_gap is None or gap < closest_gap:
                closest_gap  = gap
                closest_conflict = item

    if closest_conflict:
        student_dl    = _parse_date(closest_conflict["date"])
        suggested_by  = event_dl - timedelta(days=buffer_days)
        days_until_event_dl = (event_dl - today).days

        if event_dl <= student_dl:
            # Event deadline comes before or on the student deadline
            message = (
                f"Event deadline ({event_dl.strftime('%b %d')}) is "
                f"{closest_gap} day(s) before your "
                f"{closest_conflict['label']} ({student_dl.strftime('%b %d')}). "
                f"Aim to submit this application by {suggested_by.strftime('%b %d')} "
                f"so you can focus on your coursework."
            )
        else:
            # Student deadline comes first
            message = (
                f"Your {closest_conflict['label']} ({student_dl.strftime('%b %d')}) "
                f"is due {closest_gap} day(s) before this event deadline "
                f"({event_dl.strftime('%b %d')}). "
                f"Complete your coursework first, then focus on this application."
            )

        return {
            "type":              "deadline_proximity",
            "event_deadline":    event_dl.isoformat(),
            "student_deadline":  closest_conflict["date"],
            "student_label":     closest_conflict["label"],
            "gap_days":          closest_gap,
            "suggested_submit_by": suggested_by.isoformat(),
            "days_until_event_deadline": days_until_event_dl,
            "message":           message
        }

    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_conflict_check(event: dict, profile: dict) -> dict:
    """
    Run both conflict checks on a single matched event.
    Adds conflict flags directly to the event dict and returns it.

    Args:
        event:   structured event dict from event_extractor
        profile: student profile dict from Cosmos DB

    Returns:
        Event dict enriched with conflict_flags and calendar_note
    """
    timetable        = profile.get("timetable", {})
    blocked_slots    = timetable.get("blocked_slots", [])
    upcoming_deadlines = timetable.get("upcoming_deadlines", [])

    event_sessions   = event.get("event_sessions", [])
    event_deadline   = event.get("deadline")

    flags = []

    # Session conflicts
    session_conflicts = check_session_conflicts(event_sessions, blocked_slots)
    flags.extend(session_conflicts)

    # Deadline proximity
    dl_warning = check_deadline_proximity(event_deadline, upcoming_deadlines)
    if dl_warning:
        flags.append(dl_warning)

    event["conflict_flags"] = flags
    event["has_conflict"]   = len(session_conflicts) > 0
    event["has_deadline_warning"] = dl_warning is not None

    # Build a single human-readable calendar note for the digest
    notes = []
    for f in session_conflicts:
        notes.append(f["message"])
    if dl_warning:
        notes.append(dl_warning["message"])

    event["calendar_note"] = " | ".join(notes) if notes else None

    return event


def run_conflict_checks_batch(events: list, profile: dict) -> list:
    """
    Run conflict checks on a list of matched events.
    Returns events sorted by: no conflict first, then deadline ascending.
    """
    checked = [run_conflict_check(e, profile) for e in events]

    def sort_key(e):
        has_conflict = 1 if e.get("has_conflict") else 0
        deadline     = e.get("deadline") or "9999-12-31"
        return (has_conflict, deadline)

    checked.sort(key=sort_key)
    return checked
