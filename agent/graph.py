from __future__ import annotations

import logging
import os
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
USER_EMAIL = os.getenv("GRAPH_USER_ID", "hku.demo.agent@outlook.com")

# Protected senders — never archive regardless of content
PROTECTED_SENDERS = [
    "registry.hku.hk", "aaoffice.hku.hk", "cedars.hku.hk",
    "financial-aid.hku.hk", "scholarships@hku.hk", "aaso@hku.hk"
]


def get_access_token() -> str:
    tenant_id     = os.getenv("GRAPH_TENANT_ID")
    client_id     = os.getenv("GRAPH_CLIENT_ID")
    client_secret = os.getenv("GRAPH_CLIENT_SECRET")

    if not all([tenant_id, client_id, client_secret]):
        raise RuntimeError("Missing GRAPH_TENANT_ID, GRAPH_CLIENT_ID, or GRAPH_CLIENT_SECRET")

    url  = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type":    "client_credentials",
        "client_id":     client_id,
        "client_secret": client_secret,
        "scope":         "https://graph.microsoft.com/.default"
    }
    resp = requests.post(url, data=data, timeout=30)
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError("Could not obtain access token")
    return token


def _headers() -> dict:
    return {"Authorization": f"Bearer {get_access_token()}",
            "Content-Type": "application/json"}


def get_unread_emails(user_email: str = USER_EMAIL) -> list:
    url    = f"{GRAPH_BASE}/users/{user_email}/mailFolders/inbox/messages"
    params = {
        "$filter": "isRead eq false",
        "$top":    25,
        "$select": "id,subject,from,receivedDateTime,bodyPreview,body,conversationId"
    }
    resp = requests.get(url, headers=_headers(), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def get_or_create_archive_folder(user_email: str = USER_EMAIL) -> str:
    """Get or create the Agent Archived folder. Returns folder ID."""
    url  = f"{GRAPH_BASE}/users/{user_email}/mailFolders"
    resp = requests.get(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    for folder in resp.json().get("value", []):
        if folder.get("displayName") == "Agent Archived":
            return folder["id"]
    # Create it
    resp = requests.post(url, headers=_headers(),
                         json={"displayName": "Agent Archived"}, timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def archive_email(email_id: str, user_email: str = USER_EMAIL) -> dict:
    """Move email to Agent Archived folder. Never deletes."""
    folder_id = get_or_create_archive_folder(user_email)
    url  = f"{GRAPH_BASE}/users/{user_email}/messages/{email_id}/move"
    resp = requests.post(url, headers=_headers(),
                         json={"destinationId": folder_id}, timeout=30)
    if resp.status_code not in (200, 201):
        return {"success": False, "new_id": ""}
    return {"success": True, "new_id": resp.json().get("id", "")}


def restore_email(email_id: str, user_email: str = USER_EMAIL) -> bool:
    """Move email back to inbox (undo archive)."""
    url  = f"{GRAPH_BASE}/users/{user_email}/messages/{email_id}/move"
    resp = requests.post(url, headers=_headers(),
                         json={"destinationId": "inbox"}, timeout=30)
    return resp.status_code in (200, 201)


def is_protected_sender(email_address: str) -> bool:
    """Check if sender should never be archived."""
    addr = email_address.lower()
    return any(p in addr for p in PROTECTED_SENDERS)


def resolve_user_email(profile_email: str | None = None) -> str:
    """Resolve Graph user id/email; demo env override takes precedence."""
    return os.getenv("GRAPH_USER_ID") or profile_email or USER_EMAIL


def get_calendar_events(
    user_email: str,
    start_datetime: str,
    end_datetime: str,
) -> dict:
    """
    Fetch calendar events in a date range via calendarView.

    Returns:
        {"success": True, "events": [...]} or {"success": False, "error": "...", "events": []}
    """
    url = f"{GRAPH_BASE}/users/{user_email}/calendar/calendarView"
    params = {
        "startDateTime": start_datetime,
        "endDateTime": end_datetime,
        "$select": "subject,start,end,location,isAllDay,recurrence",
        "$top": 250,
    }
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=30)
        if resp.status_code != 200:
            return {
                "success": False,
                "error": f"Calendar fetch failed ({resp.status_code})",
                "events": [],
            }
        return {"success": True, "events": resp.json().get("value", []), "error": ""}
    except Exception as exc:
        logger.error(f"get_calendar_events error: {exc}")
        return {"success": False, "error": str(exc), "events": []}


def create_calendar_event(
    user_email: str,
    title: str,
    start_iso: str,
    end_iso: str,
    location: str = "",
    timezone: str = "Asia/Hong_Kong",
) -> dict:
    """
    Create an Outlook calendar event for the user.

    Returns:
        {"success": True, "event_id": "...", "web_link": "..."}
        or {"success": False, "error": "..."}
    """
    body = {
        "subject": title,
        "start": {"dateTime": start_iso, "timeZone": timezone},
        "end": {"dateTime": end_iso, "timeZone": timezone},
    }
    if location:
        body["location"] = {"displayName": location}

    url = f"{GRAPH_BASE}/users/{user_email}/calendar/events"
    try:
        resp = requests.post(url, headers=_headers(), json=body, timeout=30)
        if resp.status_code not in (200, 201):
            return {
                "success": False,
                "error": f"Could not create calendar event ({resp.status_code})",
            }
        data = resp.json()
        return {
            "success": True,
            "event_id": data.get("id", ""),
            "web_link": data.get("webLink", ""),
            "error": "",
        }
    except Exception as exc:
        logger.error(f"create_calendar_event error: {exc}")
        return {"success": False, "error": str(exc)}


_WEEKDAYS = (
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
)


def _parse_graph_datetime(value: str) -> tuple[str, str] | None:
    """Parse Graph dateTime string into (weekday_name, HH:MM)."""
    if not value:
        return None
    try:
        normalized = value.split(".")[0]
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        dt = datetime.fromisoformat(normalized)
        return _WEEKDAYS[dt.weekday()], f"{dt.hour:02d}:{dt.minute:02d}"
    except (ValueError, TypeError):
        return None


def calendar_events_to_blocked_slots(events: list, max_slots: int = 10) -> list:
    """
    Convert Graph calendar events into timetable blocked_slots for conflict checking.
    Deduplicates recurring instances by (subject, day, start, end).
    """
    seen = set()
    slots = []
    for event in events or []:
        if event.get("isAllDay"):
            continue
        start_info = event.get("start") or {}
        end_info = event.get("end") or {}
        start_parsed = _parse_graph_datetime(start_info.get("dateTime", ""))
        end_parsed = _parse_graph_datetime(end_info.get("dateTime", ""))
        if not start_parsed or not end_parsed:
            continue
        day, start_time = start_parsed
        _, end_time = end_parsed
        subject = (event.get("subject") or "Calendar event").strip()
        key = (subject.lower(), day.lower(), start_time, end_time)
        if key in seen:
            continue
        seen.add(key)
        slots.append({
            "day": day,
            "start": start_time,
            "end": end_time,
            "label": subject,
        })
        if len(slots) >= max_slots:
            break
    return slots
