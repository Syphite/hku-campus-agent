from __future__ import annotations

import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
ME_BASE = f"{GRAPH_BASE}/me"

# Protected senders — never archive regardless of content
PROTECTED_SENDERS = [
    "registry.hku.hk", "aaoffice.hku.hk", "cedars.hku.hk",
    "financial-aid.hku.hk", "scholarships@hku.hk", "aaso@hku.hk"
]

SIGN_IN_HINT = "Complete Microsoft sign-in to access your mail and calendar."


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


def _require_user_token(user_token: str | None) -> str:
    if not user_token or not str(user_token).strip():
        raise GraphApiError(
            "Graph sign-in required",
            hint=SIGN_IN_HINT,
        )
    return str(user_token).strip()


def _delegated_headers(user_token: str) -> dict:
    return {
        "Authorization": f"Bearer {_require_user_token(user_token)}",
        "Content-Type": "application/json",
    }


def _delegated_failure_hint(status: int, url: str) -> str:
    if "/me/" in url and status == 401:
        return "Your Microsoft sign-in may have expired — please sign in again."
    if "/mailFolders/" in url and status == 401:
        return SIGN_IN_HINT
    if "/calendar/" in url and status == 401:
        return SIGN_IN_HINT
    if status == 403:
        return "Graph returned forbidden — check Mail/Calendar delegated permissions and consent."
    return "Graph request failed"


def _graph_request(
    method: str,
    url: str,
    *,
    user_token: str,
    params: dict | None = None,
    json_body: dict | None = None,
    timeout: int = 30,
) -> requests.Response:
    """Execute a delegated Graph /me request; raise GraphApiError on failure."""
    from agent.graph_diagnostics import parse_graph_error_response

    resp = requests.request(
        method.upper(),
        url,
        headers=_delegated_headers(user_token),
        params=params,
        json=json_body,
        timeout=timeout,
    )
    if resp.ok:
        return resp

    parsed = parse_graph_error_response(resp)
    hint = _delegated_failure_hint(parsed.get("status") or resp.status_code, url)

    err = GraphApiError(
        f"Graph request failed: {method.upper()} {url} ({resp.status_code})",
        method=method.upper(),
        url=url,
        status=parsed.get("status") or resp.status_code,
        error_code=parsed.get("error_code") or "",
        error_message=parsed.get("error_message") or resp.text[:300],
        request_id=parsed.get("request_id") or "",
        user_id="me",
        hint=hint,
        www_authenticate=parsed.get("www_authenticate") or "",
    )
    logger.error(
        "Graph request failed: %s %s\n"
        "  status=%s\n"
        "  error_code=%s\n"
        "  error_message=%s\n"
        "  request_id=%s\n"
        "  hint=%s",
        err.method,
        err.url,
        err.status,
        err.error_code,
        err.error_message,
        err.request_id,
        err.hint,
    )
    raise err


def get_unread_emails(user_token: str) -> list:
    url = f"{ME_BASE}/mailFolders/inbox/messages"
    params = {
        "$filter": "isRead eq false",
        "$top": 25,
        "$select": "id,subject,from,receivedDateTime,bodyPreview,body,conversationId",
    }
    resp = _graph_request("GET", url, user_token=user_token, params=params)
    return resp.json().get("value", [])


def get_or_create_archive_folder(user_token: str) -> str:
    """Get or create the Agent Archived folder. Returns folder ID."""
    url = f"{ME_BASE}/mailFolders"
    resp = _graph_request("GET", url, user_token=user_token)
    for folder in resp.json().get("value", []):
        if folder.get("displayName") == "Agent Archived":
            return folder["id"]
    resp = _graph_request(
        "POST",
        url,
        user_token=user_token,
        json_body={"displayName": "Agent Archived"},
    )
    return resp.json()["id"]


def archive_email(email_id: str, user_token: str) -> dict:
    """Move email to Agent Archived folder. Never deletes."""
    try:
        folder_id = get_or_create_archive_folder(user_token)
        url = f"{ME_BASE}/messages/{email_id}/move"
        resp = _graph_request(
            "POST",
            url,
            user_token=user_token,
            json_body={"destinationId": folder_id},
        )
        return {"success": True, "new_id": resp.json().get("id", "")}
    except GraphApiError as exc:
        logger.error("archive_email failed: %s", exc.to_log_dict())
        return {"success": False, "new_id": ""}


def restore_email(email_id: str, user_token: str) -> bool:
    """Move email back to inbox (undo archive)."""
    url = f"{ME_BASE}/messages/{email_id}/move"
    try:
        _graph_request(
            "POST",
            url,
            user_token=user_token,
            json_body={"destinationId": "inbox"},
        )
        return True
    except GraphApiError as exc:
        logger.error("restore_email failed: %s", exc.to_log_dict())
        return False


def is_protected_sender(email_address: str) -> bool:
    """Check if sender should never be archived."""
    addr = email_address.lower()
    return any(p in addr for p in PROTECTED_SENDERS)


def get_calendar_events(
    user_token: str,
    start_datetime: str,
    end_datetime: str,
) -> dict:
    """
    Fetch calendar events in a date range via calendarView.

    Returns:
        {"success": True, "events": [...]} or {"success": False, "error": "...", "events": []}
    """
    url = f"{ME_BASE}/calendar/calendarView"
    params = {
        "startDateTime": start_datetime,
        "endDateTime": end_datetime,
        "$select": "subject,start,end,location,isAllDay,recurrence",
        "$top": 250,
    }
    try:
        resp = _graph_request("GET", url, user_token=user_token, params=params)
        return {"success": True, "events": resp.json().get("value", []), "error": ""}
    except GraphApiError as exc:
        logger.error("get_calendar_events failed: %s", exc.to_log_dict())
        return {
            "success": False,
            "error": f"Calendar fetch failed ({exc.status}): {exc.error_code or exc.error_message}",
            "events": [],
        }
    except Exception as exc:
        logger.error("get_calendar_events error: %s", exc)
        return {"success": False, "error": str(exc), "events": []}


def create_calendar_event(
    user_token: str,
    title: str,
    start_iso: str,
    end_iso: str,
    location: str = "",
    timezone: str = "Asia/Hong_Kong",
) -> dict:
    """
    Create an Outlook calendar event for the signed-in user.

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

    url = f"{ME_BASE}/calendar/events"
    try:
        resp = _graph_request("POST", url, user_token=user_token, json_body=body)
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
        logger.error("create_calendar_event error: %s", exc)
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
