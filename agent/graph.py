from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

from agent.datetime_utils import parse_graph_datetime_field, validate_iso_datetime

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
ME_BASE = f"{GRAPH_BASE}/me"

AGENT_FOLDER_NOISE = "Agent Archived"
AGENT_FOLDER_AMBIGUOUS = "Agent Ambiguous"
HK_TZ = timezone(timedelta(hours=8))
MESSAGE_SELECT = "id,subject,from,isRead,receivedDateTime,bodyPreview,body,conversationId,webLink"

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
    return get_all_unread_emails(user_token)


def hk_today_start_utc_iso() -> str:
    start = datetime.now(HK_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _graph_get_paged(
    user_token: str,
    url: str,
    *,
    params: dict | None = None,
    max_messages: int | None = None,
) -> list:
    headers = _delegated_headers(user_token)
    messages: list = []
    next_url: str | None = url
    next_params = params

    while next_url and (max_messages is None or len(messages) < max_messages):
        resp = requests.get(next_url, headers=headers, params=next_params, timeout=30)
        if not resp.ok:
            from agent.graph_diagnostics import parse_graph_error_response

            parsed = parse_graph_error_response(resp)
            hint = _delegated_failure_hint(parsed.get("status") or resp.status_code, next_url)
            raise GraphApiError(
                f"Graph paged request failed: GET {next_url} ({resp.status_code})",
                method="GET",
                url=next_url,
                status=parsed.get("status") or resp.status_code,
                error_code=parsed.get("error_code") or "",
                error_message=parsed.get("error_message") or resp.text[:300],
                request_id=parsed.get("request_id") or "",
                user_id="me",
                hint=hint,
            )
        data = resp.json()
        messages.extend(data.get("value", []))
        next_url = data.get("@odata.nextLink")
        next_params = None

    if max_messages is None:
        return messages
    return messages[:max_messages]


def _sort_emails_newest_first(emails: list) -> list:
    return sorted(
        emails,
        key=lambda email: email.get("receivedDateTime") or "",
        reverse=True,
    )


def get_all_unread_emails(user_token: str, *, max_messages: int | None = None) -> list:
    url = f"{ME_BASE}/mailFolders/inbox/messages"
    params = {
        "$filter": "isRead eq false",
        "$top": 50,
        "$select": MESSAGE_SELECT,
        "$orderby": "receivedDateTime desc",
    }
    emails = _graph_get_paged(user_token, url, params=params, max_messages=max_messages)
    return _sort_emails_newest_first(emails)


def get_inbox_messages_since(user_token: str, since_iso: str, *, max_messages: int = 100) -> list:
    url = f"{ME_BASE}/mailFolders/inbox/messages"
    params = {
        "$filter": f"receivedDateTime ge {since_iso}",
        "$top": 50,
        "$select": MESSAGE_SELECT,
        "$orderby": "receivedDateTime desc",
    }
    return _graph_get_paged(user_token, url, params=params, max_messages=max_messages)


def get_me_profile(user_token: str) -> dict:
    """Signed-in user's mailbox identity via delegated /me."""
    resp = _graph_request(
        "GET",
        ME_BASE,
        user_token=user_token,
        params={"$select": "displayName,mail,userPrincipalName"},
    )
    return resp.json()


def get_inbox_folder_stats(user_token: str) -> dict:
    """Inbox folder counts from Graph."""
    resp = _graph_request(
        "GET",
        f"{ME_BASE}/mailFolders/inbox",
        user_token=user_token,
        params={"$select": "displayName,totalItemCount,unreadItemCount,id"},
    )
    return resp.json()


def get_inbox_messages(
    user_token: str,
    *,
    top: int = 25,
    unread_only: bool = False,
) -> list:
    """Fetch inbox messages (newest first)."""
    url = f"{ME_BASE}/mailFolders/inbox/messages"
    params = {
        "$top": top,
        "$select": MESSAGE_SELECT,
        "$orderby": "receivedDateTime desc",
    }
    if unread_only:
        params["$filter"] = "isRead eq false"
    resp = _graph_request("GET", url, user_token=user_token, params=params)
    return resp.json().get("value", [])


def is_protected_sender(email_address: str) -> bool:
    """Check if sender should never be archived."""
    addr = email_address.lower()
    return any(p in addr for p in PROTECTED_SENDERS)


def is_protected_email(sender: str, subject: str = "", preview: str = "") -> bool:
    """Senders and CEDARS-on-behalf messages are never archived."""
    if is_protected_sender(sender):
        return True
    text = f"{subject or ''} {preview or ''}".lower()
    return "on behalf of cedars" in text


def _list_mail_folders(user_token: str, parent_folder_id: str | None = None) -> list:
    if parent_folder_id:
        url = f"{ME_BASE}/mailFolders/{parent_folder_id}/childFolders"
    else:
        url = f"{ME_BASE}/mailFolders"
    resp = _graph_request("GET", url, user_token=user_token, params={"$top": 100})
    return resp.json().get("value", [])


def _deleted_items_folder_id(user_token: str) -> str | None:
    try:
        resp = _graph_request(
            "GET",
            f"{ME_BASE}/mailFolders/deleteditems",
            user_token=user_token,
            params={"$select": "id"},
        )
        return resp.json().get("id")
    except GraphApiError:
        return None


def _folder_ids_under(user_token: str, root_folder_id: str) -> set[str]:
    """All folder IDs in a subtree (including root)."""
    ids = {root_folder_id}
    stack = [root_folder_id]
    while stack:
        parent_id = stack.pop()
        for folder in _list_mail_folders(user_token, parent_id):
            folder_id = folder.get("id")
            if folder_id and folder_id not in ids:
                ids.add(folder_id)
                stack.append(folder_id)
    return ids


def _deleted_items_subtree_ids(user_token: str) -> set[str]:
    deleted_root = _deleted_items_folder_id(user_token)
    if not deleted_root:
        return set()
    return _folder_ids_under(user_token, deleted_root)


def find_mail_folder_id(user_token: str, display_name: str) -> str | None:
    """Find a mail folder by display name, excluding folders under Deleted Items."""
    deleted_ids = _deleted_items_subtree_ids(user_token)

    for folder in _list_mail_folders(user_token, None):
        folder_id = folder.get("id")
        if folder.get("displayName") == display_name and folder_id and folder_id not in deleted_ids:
            return folder_id

    def search(parent_id: str | None) -> str | None:
        for folder in _list_mail_folders(user_token, parent_id):
            folder_id = folder.get("id")
            if not folder_id or folder_id in deleted_ids:
                continue
            if folder.get("displayName") == display_name:
                return folder_id
            child_id = search(folder_id)
            if child_id:
                return child_id
        return None

    return search(None)


def get_or_create_mail_folder(user_token: str, display_name: str) -> str:
    existing = find_mail_folder_id(user_token, display_name)
    if existing:
        return existing
    url = f"{ME_BASE}/mailFolders"
    resp = _graph_request(
        "POST",
        url,
        user_token=user_token,
        json_body={"displayName": display_name},
    )
    folder_id = resp.json().get("id")
    if not folder_id:
        raise GraphApiError(
            f"Could not create mail folder {display_name}",
            method="POST",
            url=url,
            status=resp.status_code,
        )
    logger.info("Created mail folder %s (%s)", display_name, folder_id)
    return folder_id


def ensure_agent_mail_folders(user_token: str) -> dict:
    """Pre-create agent triage folders so both exist before moving mail."""
    return {
        "archive": get_or_create_mail_folder(user_token, AGENT_FOLDER_NOISE),
        "ambiguous": get_or_create_mail_folder(user_token, AGENT_FOLDER_AMBIGUOUS),
    }


def get_agent_folder_stats(user_token: str) -> list[dict]:
    """Return message counts for agent-managed folders (for diagnostics)."""
    stats = []
    for name in (AGENT_FOLDER_NOISE, AGENT_FOLDER_AMBIGUOUS):
        folder_id = find_mail_folder_id(user_token, name)
        if not folder_id:
            stats.append({"name": name, "exists": False, "total": 0, "unread": 0})
            continue
        resp = _graph_request(
            "GET",
            f"{ME_BASE}/mailFolders/{folder_id}",
            user_token=user_token,
            params={"$select": "displayName,totalItemCount,unreadItemCount,id"},
        )
        folder = resp.json()
        stats.append({
            "name": folder.get("displayName") or name,
            "exists": True,
            "total": folder.get("totalItemCount", 0),
            "unread": folder.get("unreadItemCount", 0),
        })
    return stats


def get_or_create_archive_folder(user_token: str) -> str:
    return get_or_create_mail_folder(user_token, AGENT_FOLDER_NOISE)


def get_or_create_ambiguous_folder(user_token: str) -> str:
    return get_or_create_mail_folder(user_token, AGENT_FOLDER_AMBIGUOUS)


def move_email_to_folder(email_id: str, folder_id: str, user_token: str) -> dict:
    try:
        url = f"{ME_BASE}/messages/{email_id}/move"
        resp = _graph_request(
            "POST",
            url,
            user_token=user_token,
            json_body={"destinationId": folder_id},
        )
        return {"success": True, "new_id": resp.json().get("id", "")}
    except GraphApiError as exc:
        logger.error("move_email_to_folder failed: %s", exc.to_log_dict())
        return {"success": False, "new_id": ""}


def archive_email(email_id: str, user_token: str) -> dict:
    """Move email to Agent Archived folder. Never deletes."""
    folder_id = get_or_create_archive_folder(user_token)
    return move_email_to_folder(email_id, folder_id, user_token)


def move_to_ambiguous_folder(email_id: str, user_token: str) -> dict:
    """Move email to Agent Ambiguous folder for human review."""
    folder_id = get_or_create_ambiguous_folder(user_token)
    return move_email_to_folder(email_id, folder_id, user_token)


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
    if not validate_iso_datetime(start_iso) or not validate_iso_datetime(end_iso):
        return {"success": False, "error": f"Invalid event datetime: start={start_iso!r} end={end_iso!r}"}
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


def delete_calendar_event(user_token: str, event_id: str) -> dict:
    """Delete an Outlook calendar event for the signed-in user."""
    if not event_id:
        return {"success": False, "error": "Missing calendar event id"}
    url = f"{ME_BASE}/calendar/events/{event_id}"
    try:
        _graph_request("DELETE", url, user_token=user_token)
        return {"success": True, "error": ""}
    except GraphApiError as exc:
        logger.error("delete_calendar_event failed: %s", exc.to_log_dict())
        return {
            "success": False,
            "error": f"Could not delete calendar event ({exc.status}): {exc.error_code or exc.error_message}",
        }
    except Exception as exc:
        logger.error("delete_calendar_event error: %s", exc)
        return {"success": False, "error": str(exc)}


def create_recurring_calendar_event(
    user_token: str,
    title: str,
    day_of_week: str,
    start_iso: str,
    end_iso: str,
    range_start: str,
    range_end: str,
    timezone: str = "Asia/Hong_Kong",
) -> dict:
    """
    Create a weekly recurring Outlook event spanning range_start..range_end (YYYY-MM-DD).
    """
    if not validate_iso_datetime(start_iso) or not validate_iso_datetime(end_iso):
        return {
            "success": False,
            "error": f"Invalid recurring event datetime: start={start_iso!r} end={end_iso!r}",
        }
    body = {
        "subject": title,
        "start": {"dateTime": start_iso, "timeZone": timezone},
        "end": {"dateTime": end_iso, "timeZone": timezone},
        "recurrence": {
            "pattern": {
                "type": "weekly",
                "interval": 1,
                "daysOfWeek": [day_of_week.lower()],
            },
            "range": {
                "type": "endDate",
                "startDate": range_start,
                "endDate": range_end,
            },
        },
    }
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
        logger.error("create_recurring_calendar_event failed: %s", exc.to_log_dict())
        return {
            "success": False,
            "error": f"Could not create recurring event ({exc.status}): {exc.error_code or exc.error_message}",
        }
    except Exception as exc:
        logger.error("create_recurring_calendar_event error: %s", exc)
        return {"success": False, "error": str(exc)}


_WEEKDAYS = (
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
)


def _parse_graph_datetime(value: str, time_zone: str = "Asia/Hong_Kong") -> tuple[str, str] | None:
    """Parse Graph dateTime string into (weekday_name, HH:MM) in HKT."""
    dt = parse_graph_datetime_field({"dateTime": value, "timeZone": time_zone})
    if not dt:
        return None
    return _WEEKDAYS[dt.weekday()], f"{dt.hour:02d}:{dt.minute:02d}"


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
        start_parsed = _parse_graph_datetime(
            start_info.get("dateTime", ""),
            start_info.get("timeZone", "Asia/Hong_Kong"),
        )
        end_parsed = _parse_graph_datetime(
            end_info.get("dateTime", ""),
            end_info.get("timeZone", "Asia/Hong_Kong"),
        )
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
