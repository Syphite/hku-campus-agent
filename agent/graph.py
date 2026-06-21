from __future__ import annotations

import logging
import os
from datetime import datetime

import requests
from dotenv import load_dotenv

from agent.graph_diagnostics import interpret_graph_failure, parse_graph_error_response

load_dotenv()

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
USER_EMAIL = os.getenv("GRAPH_USER_ID", "hku.demo.agent@outlook.com")

# Protected senders — never archive regardless of content
PROTECTED_SENDERS = [
    "registry.hku.hk", "aaoffice.hku.hk", "cedars.hku.hk",
    "financial-aid.hku.hk", "scholarships@hku.hk", "aaso@hku.hk"
]

_token_roles_cache: list[str] | None = None


class GraphApiError(Exception):
    """Structured Microsoft Graph API failure for logging and diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        method: str = "",
        url: str = "",
        status: int = 0,
        error_code: str = "",
        error_message: str = "",
        request_id: str = "",
        user_id: str = "",
        token_roles: list | None = None,
        hint: str = "",
        www_authenticate: str = "",
    ):
        super().__init__(message)
        self.method = method
        self.url = url
        self.status = status
        self.error_code = error_code
        self.error_message = error_message
        self.request_id = request_id
        self.user_id = user_id
        self.token_roles = token_roles or []
        self.hint = hint
        self.www_authenticate = www_authenticate

    def to_log_dict(self) -> dict:
        return {
            "method": self.method,
            "url": self.url,
            "status": self.status,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "request_id": self.request_id,
            "user_id": self.user_id,
            "token_roles": self.token_roles,
            "hint": self.hint,
            "www_authenticate": self.www_authenticate,
        }


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

    global _token_roles_cache
    try:
        from agent.graph_diagnostics import decode_jwt_payload
        payload = decode_jwt_payload(token)
        roles = payload.get("roles") or []
        _token_roles_cache = list(roles) if isinstance(roles, list) else []
    except Exception:
        _token_roles_cache = None

    return token


def _cached_token_roles() -> list[str]:
    if _token_roles_cache is None:
        return []
    return list(_token_roles_cache)


def _headers() -> dict:
    return {"Authorization": f"Bearer {get_access_token()}",
            "Content-Type": "application/json"}


def _extract_user_id_from_url(url: str) -> str:
    marker = "/users/"
    if marker not in url:
        return ""
    remainder = url.split(marker, 1)[1]
    return remainder.split("/", 1)[0]


def _graph_request(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    timeout: int = 30,
    user_id: str = "",
) -> requests.Response:
    """Execute a Graph request; raise GraphApiError with structured details on failure."""
    resp = requests.request(
        method.upper(),
        url,
        headers=_headers(),
        params=params,
        json=json_body,
        timeout=timeout,
    )
    if resp.ok:
        return resp

    parsed = parse_graph_error_response(resp)
    resolved_user = user_id or _extract_user_id_from_url(url)
    mini_report = {
        "token": {"ok": True, "roles": _cached_token_roles()},
        "user_lookup": {"ok": True} if "/mailFolders/" not in url else {"ok": True},
        "inbox_read": parsed if "/mailFolders/" in url else {"ok": True},
        "ok": False,
    }
    if "/users/" in url and "/mailFolders/" not in url and resp.status_code == 404:
        mini_report["user_lookup"] = parsed
    hints = interpret_graph_failure(mini_report)
    hint = hints[0] if hints else parsed.get("error_message") or "Graph request failed"

    err = GraphApiError(
        f"Graph request failed: {method.upper()} {url} ({resp.status_code})",
        method=method.upper(),
        url=url,
        status=parsed.get("status") or resp.status_code,
        error_code=parsed.get("error_code") or "",
        error_message=parsed.get("error_message") or resp.text[:300],
        request_id=parsed.get("request_id") or "",
        user_id=resolved_user,
        token_roles=_cached_token_roles(),
        hint=hint,
        www_authenticate=parsed.get("www_authenticate") or "",
    )
    logger.error(
        "Graph request failed: %s %s\n"
        "  status=%s\n"
        "  error_code=%s\n"
        "  error_message=%s\n"
        "  request_id=%s\n"
        "  user_id=%s\n"
        "  token_roles=%s\n"
        "  hint=%s",
        err.method,
        err.url,
        err.status,
        err.error_code,
        err.error_message,
        err.request_id,
        err.user_id,
        err.token_roles,
        err.hint,
    )
    raise err


def get_unread_emails(user_email: str = USER_EMAIL) -> list:
    url    = f"{GRAPH_BASE}/users/{user_email}/mailFolders/inbox/messages"
    params = {
        "$filter": "isRead eq false",
        "$top":    25,
        "$select": "id,subject,from,receivedDateTime,bodyPreview,body,conversationId"
    }
    resp = _graph_request("GET", url, params=params, user_id=user_email)
    return resp.json().get("value", [])


def get_or_create_archive_folder(user_email: str = USER_EMAIL) -> str:
    """Get or create the Agent Archived folder. Returns folder ID."""
    url  = f"{GRAPH_BASE}/users/{user_email}/mailFolders"
    resp = _graph_request("GET", url, user_id=user_email)
    for folder in resp.json().get("value", []):
        if folder.get("displayName") == "Agent Archived":
            return folder["id"]
    resp = _graph_request(
        "POST",
        url,
        json_body={"displayName": "Agent Archived"},
        user_id=user_email,
    )
    return resp.json()["id"]


def archive_email(email_id: str, user_email: str = USER_EMAIL) -> dict:
    """Move email to Agent Archived folder. Never deletes."""
    try:
        folder_id = get_or_create_archive_folder(user_email)
        url  = f"{GRAPH_BASE}/users/{user_email}/messages/{email_id}/move"
        resp = _graph_request(
            "POST",
            url,
            json_body={"destinationId": folder_id},
            user_id=user_email,
        )
        return {"success": True, "new_id": resp.json().get("id", "")}
    except GraphApiError as exc:
        logger.error("archive_email failed: %s", exc.to_log_dict())
        return {"success": False, "new_id": ""}


def restore_email(email_id: str, user_email: str = USER_EMAIL) -> bool:
    """Move email back to inbox (undo archive)."""
    url  = f"{GRAPH_BASE}/users/{user_email}/messages/{email_id}/move"
    try:
        _graph_request(
            "POST",
            url,
            json_body={"destinationId": "inbox"},
            user_id=user_email,
        )
        return True
    except GraphApiError as exc:
        logger.error("restore_email failed: %s", exc.to_log_dict())
        return False


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
        resp = _graph_request("GET", url, params=params, user_id=user_email)
        return {"success": True, "events": resp.json().get("value", []), "error": ""}
    except GraphApiError as exc:
        logger.error("get_calendar_events failed: %s", exc.to_log_dict())
        return {
            "success": False,
            "error": f"Calendar fetch failed ({exc.status}): {exc.error_code or exc.error_message}",
            "events": [],
        }
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
        resp = _graph_request("POST", url, json_body=body, user_id=user_email)
        data = resp.json()
        return {
            "success": True,
            "event_id": data.get("id", ""),
            "web_link": data.get("webLink", ""),
            "error": "",
        }
    except GraphApiError as exc:
        logger.error("create_calendar_event failed: %s", exc.to_log_dict())
        return {
            "success": False,
            "error": f"Could not create calendar event ({exc.status}): {exc.error_code or exc.error_message}",
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
