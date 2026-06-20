"""
agent/digest.py

Assembles the full student digest from three sources:
1. Scholarship matches (from matching.py)
2. Event matches (from event_extractor + conflict_checker)
3. Inbox summary (from email classifier — teammate's module)

Returns a unified structured digest that handler.py renders
into Copilot Chat messages and Adaptive Cards.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def assemble_digest(
    student_id: str,
    scholarship_result: dict,
    events: list,
    inbox_summary: Optional[dict] = None,
) -> dict:
    """
    Assembles the full digest for a student.

    Args:
        student_id:          student ID string
        scholarship_result:  output of run_matching() — has apply_now, prepare
        events:              output of run_conflict_checks_batch() — checked events
        inbox_summary:       output of email classifier (optional, teammate module)
                             expected shape:
                             {
                               "processed": 34,
                               "archived": 26,
                               "kept": 8,
                               "archived_items": [
                                 {"email_id": "...", "subject": "...", "reason": "..."}
                               ],
                               "relevant_items": [
                                 {"email_id": "...", "subject": "...", "category": "relevant"}
                               ]
                             }

    Returns:
        Structured digest dict ready for handler.py to render.
    """

    # ── Scholarships ────────────────────────────────────────────────────────
    apply_now = list(scholarship_result.get("apply_now", []))
    prepare   = list(scholarship_result.get("prepare", []))

    # ── Events ──────────────────────────────────────────────────────────────
    # Split into: apply now (has deadline within 30 days or no deadline)
    # and upcoming (deadline further out)
    from datetime import date
    today = date.today()

    urgent_events  = []
    upcoming_events = []

    for e in events:
        if not isinstance(e, dict):
            continue
        event_sessions = e.get("event_sessions")
        if not isinstance(event_sessions, list):
            e["event_sessions"] = []
        dl = e.get("deadline")
        days_left = None
        if dl:
            try:
                dl_date = date.fromisoformat(str(dl)[:10])
                days_left = (dl_date - today).days
            except ValueError:
                days_left = None

        if str(e.get("type", "")).lower() == "scholarship":
            # Scholarship applications are handled by Azure AI Search, not event feeds.
            continue

        if days_left is not None and days_left <= 30:
            urgent_events.append(e)
        else:
            upcoming_events.append(e)

    # Sort urgent events by deadline ascending
    urgent_events.sort(key=lambda e: e.get("deadline") or "9999-12-31")

    # ── Inbox ────────────────────────────────────────────────────────────────
    if inbox_summary is None:
        # Teammate module not yet connected — use placeholder
        inbox_summary = {
            "processed": 0,
            "archived":  0,
            "kept":      0,
            "archived_items":  [],
            "relevant_items":  [],
            "note": "Email integration coming soon"
        }

    # ── Assemble ─────────────────────────────────────────────────────────────
    digest = {
        "student_id":   student_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),

        "scholarships": {
            "apply_now":       apply_now,
            "prepare":         prepare,
            "apply_now_count": len(apply_now),
            "prepare_count":   len(prepare),
        },

        "events": {
            "urgent":          urgent_events,
            "upcoming":        upcoming_events,
            "urgent_count":    len(urgent_events),
            "upcoming_count":  len(upcoming_events),
        },

        "inbox": inbox_summary,

        # Top-level summary for the opening message
        "summary": {
            "scholarships_open":    len(apply_now),
            "scholarships_prepare": len(prepare),
            "events_urgent":        len(urgent_events),
            "events_upcoming":      len(upcoming_events),
            "emails_processed":     inbox_summary.get("processed", 0),
            "emails_archived":      inbox_summary.get("archived", 0),
        }
    }

    logger.info(
        f"Digest assembled for {student_id}: "
        f"{len(apply_now)} scholarships open, "
        f"{len(urgent_events)} urgent events, "
        f"{inbox_summary.get('processed', 0)} emails processed"
    )

    return digest


def format_digest_message(digest: dict) -> str:
    """
    Formats the digest into a plain text summary message
    for the opening Copilot Chat response.
    The detailed cards are sent separately by handler.py.
    """
    s  = digest["summary"]
    lines = ["Here's your update:\n"]

    if s["scholarships_open"] > 0:
        lines.append(
            f"📋 **{s['scholarships_open']} scholarship(s) open now** — "
            f"deadline approaching. Tap to start a draft."
        )
    if s["scholarships_prepare"] > 0:
        lines.append(
            f"📅 **{s['scholarships_prepare']} scholarship(s) to prepare for** — "
            f"not yet open but worth getting ready."
        )
    if s["events_urgent"] > 0:
        lines.append(
            f"🏆 **{s['events_urgent']} event(s) with upcoming deadlines** — "
            f"competitions and events closing soon."
        )
    if s["events_upcoming"] > 0:
        lines.append(
            f"📌 **{s['events_upcoming']} upcoming event(s)** worth bookmarking."
        )
    if s["emails_processed"] > 0:
        lines.append(
            f"📬 **Inbox:** {s['emails_processed']} emails processed, "
            f"{s['emails_archived']} archived. "
            f"Your inbox is clean."
        )
    if len(lines) == 1:
        lines.append("Nothing new to report today — check back soon.")

    return "\n".join(lines)

