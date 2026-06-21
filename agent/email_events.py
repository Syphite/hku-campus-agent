"""Convert classified inbox items into digest event entries."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

_EVENT_KEYWORDS = (
    "talk", "seminar", "workshop", "competition", "hackathon", "career fair",
    "info session", "briefing", "registration", "deadline", "submit", "event",
    "lecture", "webinar", "forum", "symposium",
)


def outlook_message_url(item: dict) -> str:
    link = str(item.get("web_link") or item.get("webLink") or "").strip()
    if link:
        return link
    email_id = str(item.get("email_id") or item.get("original_id") or "").strip()
    if not email_id:
        return ""
    return f"https://outlook.office365.com/owa/?ItemID={quote(email_id, safe='')}&exvsurl=1&viewmodel=ReadMessageItem"


def _infer_event_type(subject: str, preview: str) -> str:
    text = f"{subject} {preview}".lower()
    if "hackathon" in text:
        return "hackathon"
    if "competition" in text or "contest" in text:
        return "competition"
    if "career fair" in text:
        return "career_fair"
    if "internship" in text:
        return "internship"
    if "workshop" in text:
        return "workshop"
    if any(term in text for term in ("talk", "seminar", "lecture", "webinar")):
        return "talk"
    return "other"


def _looks_event_like(item: dict) -> bool:
    if item.get("timing"):
        return True
    text = f"{item.get('subject', '')} {item.get('body_preview', '')}".lower()
    return any(keyword in text for keyword in _EVENT_KEYWORDS)


def _timing_to_sessions(timing: dict) -> list[dict]:
    start_iso = timing.get("start_iso") or ""
    end_iso = timing.get("end_iso") or ""
    if not start_iso or not end_iso:
        return []
    try:
        start_dt = datetime.fromisoformat(str(start_iso).split(".")[0])
        end_dt = datetime.fromisoformat(str(end_iso).split(".")[0])
    except ValueError:
        return []
    return [{
        "day": start_dt.strftime("%A"),
        "start": start_dt.strftime("%H:%M"),
        "end": end_dt.strftime("%H:%M"),
        "label": "From email",
    }]


def _build_summary(item: dict) -> str:
    preview = str(item.get("body_preview") or "").strip()
    if preview:
        return preview[:120] + ("..." if len(preview) > 120 else "")
    actions = item.get("action_items") or []
    if actions:
        return str(actions[0])[:120]
    return str(item.get("reason") or "From your inbox")[:120]


def inbox_item_to_event(item: dict) -> dict | None:
    """Map one enriched inbox item to a digest event dict."""
    if not _looks_event_like(item):
        return None

    subject = str(item.get("subject") or "Email event").strip()
    timing = item.get("timing") or {}
    deadline = timing.get("deadline_date") or ""
    if not deadline and timing.get("end_iso"):
        deadline = str(timing["end_iso"])[:10]

    email_url = outlook_message_url(item)
    organiser = str(item.get("from") or "Email").strip()
    if "@" in organiser:
        organiser = organiser.split("@")[0].replace(".", " ").title()

    return {
        "source_id": f"email_{item.get('email_id', '')}",
        "id": f"email_{item.get('email_id', '')}",
        "source": "email",
        "source_url": email_url,
        "type": _infer_event_type(subject, item.get("body_preview", "")),
        "title": subject,
        "organiser": organiser,
        "deadline": deadline or None,
        "deadline_display": timing.get("deadline_display") or deadline or "See email",
        "event_sessions": _timing_to_sessions(timing),
        "eligibility": "From your HKU inbox",
        "location": "See email",
        "summary": _build_summary(item),
        "match_reason": str(item.get("reason") or "Found in your inbox").strip(),
        "calendar_note": item.get("calendar_note"),
        "email_id": item.get("email_id"),
    }


def _event_dedupe_key(event: dict) -> str:
    if event.get("source") == "email":
        email_id = str(event.get("email_id") or "").strip()
        if email_id:
            return f"email:{email_id}"
        return f"email:{str(event.get('title') or '').strip().lower()}"
    source_id = str(event.get("source_id") or event.get("id") or "").strip()
    if source_id:
        return source_id.lower()
    return str(event.get("title") or "").strip().lower()


def dedupe_events(events: list[dict]) -> list[dict]:
    seen: set[str] = set()
    deduped: list[dict] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        key = _event_dedupe_key(event)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(event)
    return deduped


def inbox_items_to_events(urgent_items: list, relevant_items: list) -> list[dict]:
    """Build event list from inbox urgent + relevant items (deduped by email_id)."""
    events = []
    seen_ids = set()
    seen_fingerprints = set()
    for item in (urgent_items or []) + (relevant_items or []):
        if not isinstance(item, dict):
            continue
        email_id = item.get("email_id")
        fingerprint = f"{str(item.get('from') or '').lower()}|{str(item.get('subject') or '').lower()}"
        if email_id in seen_ids or fingerprint in seen_fingerprints:
            continue
        event = inbox_item_to_event(item)
        if not event:
            continue
        if email_id:
            seen_ids.add(email_id)
        seen_fingerprints.add(fingerprint)
        events.append(event)
    return events
