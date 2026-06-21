"""
Delegated Graph diagnostics for the signed-in Copilot user.
Used by debug inbox / debug calendar chat commands.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent.email_dedup import load_archived_fingerprints
from agent.email_pipeline import fetch_inbox_candidates
from agent.graph import (
    GraphApiError,
    calendar_events_to_blocked_slots,
    get_agent_folder_stats,
    get_calendar_events,
    get_inbox_folder_stats,
    get_inbox_messages,
    get_me_profile,
)
from agent.graph_diagnostics import decode_jwt_payload


def _sender_address(email: dict) -> str:
    return (email.get("from") or {}).get("emailAddress", {}).get("address", "") or "(unknown)"


def _shorten(text: str, limit: int = 72) -> str:
    cleaned = (text or "").replace("\n", " ").strip()
    if len(cleaned) <= limit:
        return cleaned or "(no subject)"
    return f"{cleaned[: limit - 1]}…"


def _token_summary(user_token: str, profile: dict) -> dict:
    payload = decode_jwt_payload(user_token)
    scopes = payload.get("scp") or payload.get("scope") or ""
    if isinstance(scopes, list):
        scope_list = scopes
    else:
        scope_list = [s for s in str(scopes).split() if s]
    return {
        "upn_from_token": payload.get("upn") or payload.get("preferred_username") or "",
        "name_from_token": payload.get("name") or "",
        "scopes": scope_list,
        "expires_at": profile.get("graph_token_expires_at") or "",
        "saved_at": profile.get("graph_token_saved_at") or "",
    }


def _pipeline_filter_description(scan_mode: str) -> str:
    if scan_mode == "unread_scan":
        return "isRead eq false, newest first, fully paginated (all unread)"
    return scan_mode or "unread inbox"


def _inbox_likely_causes(stats: dict) -> list[str]:
    causes = []
    candidates = int(stats.get("candidates_fetched") or 0)
    folder_unread = stats.get("folder_unread")
    folder_total = stats.get("folder_total")
    archive_exists = stats.get("agent_archive_exists")
    ambiguous_exists = stats.get("agent_ambiguous_exists")

    if folder_unread == 0 and folder_total and folder_total > 0:
        causes.append("Your inbox has messages, but **none are unread**. The pipeline only processes unread mail.")
    elif folder_unread == 0 and folder_total == 0:
        causes.append("Graph returned an **empty inbox folder** for this signed-in account.")
    elif candidates == 0 and folder_unread and folder_unread > 0:
        causes.append("Graph reports unread mail, but the unread query returned nothing — possible sync delay.")

    if archive_exists is False:
        causes.append("**Agent Archived** folder not found yet — run **inbox** to create it when noise mail is archived.")
    if ambiguous_exists is False:
        causes.append("**Agent Ambiguous** folder not found yet — it is created on first ambiguous move.")

    if not causes:
        causes.append(
            "Unread mail is fetched each run. Duplicate re-sends (same sender + subject as archived mail) "
            "are collapsed automatically — no message-ID blocklist."
        )
    return causes


def run_inbox_diagnostics(user_token: str, profile: dict) -> dict:
    """Inspect mailbox identity, folder counts, and pipeline visibility."""
    archived_fingerprints = load_archived_fingerprints(profile)

    result = {
        "ok": True,
        "error": "",
        "account": {},
        "token": _token_summary(user_token, profile),
        "folder": {},
        "agent_folders": [],
        "stats": {},
        "candidate_sample": [],
        "recent_sample": [],
        "likely_causes": [],
    }

    try:
        me = get_me_profile(user_token)
        result["account"] = {
            "display_name": me.get("displayName") or "",
            "mail": me.get("mail") or "",
            "user_principal_name": me.get("userPrincipalName") or "",
        }
    except GraphApiError as exc:
        result["ok"] = False
        result["error"] = f"/me failed ({exc.status}): {exc.error_code or exc.error_message}"
        return result

    try:
        folder = get_inbox_folder_stats(user_token)
        result["folder"] = {
            "name": folder.get("displayName") or "Inbox",
            "total_item_count": folder.get("totalItemCount"),
            "unread_item_count": folder.get("unreadItemCount"),
            "folder_id": folder.get("id") or "",
        }
    except GraphApiError as exc:
        result["error"] = f"Inbox folder stats failed ({exc.status}): {exc.error_code or exc.error_message}"

    try:
        result["agent_folders"] = get_agent_folder_stats(user_token)
    except GraphApiError:
        pass

    candidates = []
    scan_mode = ""
    try:
        candidates, scan_mode = fetch_inbox_candidates(user_token, profile)
    except GraphApiError as exc:
        result["ok"] = False
        result["error"] = result["error"] or f"Inbox fetch failed ({exc.status}): {exc.error_code or exc.error_message}"
        return result

    for email in candidates:
        eid = str(email.get("id") or "")
        result["candidate_sample"].append({
            "id": eid[:24] + "…" if len(eid) > 24 else eid,
            "subject": _shorten(email.get("subject", "")),
            "from": _sender_address(email),
            "received": (email.get("receivedDateTime") or "")[:19],
            "is_read": bool(email.get("isRead")),
            "preview": _shorten(email.get("bodyPreview", ""), 100),
        })

    try:
        recent = get_inbox_messages(user_token, top=8, unread_only=False)
        for email in recent:
            result["recent_sample"].append({
                "subject": _shorten(email.get("subject", "")),
                "from": _sender_address(email),
                "received": (email.get("receivedDateTime") or "")[:19],
                "is_read": bool(email.get("isRead")),
            })
    except GraphApiError:
        pass

    agent_folders = result.get("agent_folders") or []
    archive_folder = next((f for f in agent_folders if f.get("name") == "Agent Archived"), {})
    ambiguous_folder = next((f for f in agent_folders if f.get("name") == "Agent Ambiguous"), {})

    result["stats"] = {
        "archived_content_fingerprints": len(archived_fingerprints),
        "scan_mode": scan_mode,
        "candidates_fetched": len(candidates),
        "would_process_now": len(candidates),
        "folder_total": result["folder"].get("total_item_count"),
        "folder_unread": result["folder"].get("unread_item_count"),
        "pipeline_filter": _pipeline_filter_description(scan_mode),
        "agent_archive_exists": archive_folder.get("exists", False),
        "agent_archive_total": archive_folder.get("total", 0),
        "agent_ambiguous_exists": ambiguous_folder.get("exists", False),
        "agent_ambiguous_total": ambiguous_folder.get("total", 0),
    }
    result["likely_causes"] = _inbox_likely_causes(result["stats"])
    return result


def run_calendar_diagnostics(user_token: str, *, preview_days: int = 14, import_days: int = 120) -> dict:
    """Inspect calendar events for preview window and timetable-import window."""
    hk = timezone(timedelta(hours=8))
    now = datetime.now(hk)

    preview_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    preview_end = preview_start + timedelta(days=preview_days)
    import_end = preview_start + timedelta(days=import_days)

    result = {
        "ok": True,
        "error": "",
        "preview_range": {
            "start": preview_start.isoformat(),
            "end": preview_end.isoformat(),
            "days": preview_days,
        },
        "import_range": {
            "start": preview_start.isoformat(),
            "end": import_end.isoformat(),
            "days": import_days,
        },
        "preview_events": [],
        "import_events_count": 0,
        "import_blocked_slots": [],
        "likely_causes": [],
    }

    preview_result = get_calendar_events(
        user_token,
        preview_start.isoformat(),
        preview_end.replace(hour=23, minute=59, second=59).isoformat(),
    )
    if not preview_result.get("success"):
        result["ok"] = False
        result["error"] = preview_result.get("error") or "Calendar preview fetch failed"
        result["likely_causes"] = [
            "Calendar read failed — confirm OAuth connection includes **Calendars.ReadWrite** (or Calendars.Read) and sign in again.",
        ]
        return result

    for event in preview_result.get("events", [])[:12]:
        start = (event.get("start") or {}).get("dateTime") or ""
        end = (event.get("end") or {}).get("dateTime") or ""
        location = (event.get("location") or {}).get("displayName") or ""
        result["preview_events"].append({
            "subject": _shorten(event.get("subject") or "(no title)", 80),
            "start": start[:19],
            "end": end[:19],
            "all_day": bool(event.get("isAllDay")),
            "location": _shorten(location, 60),
        })

    import_result = get_calendar_events(
        user_token,
        preview_start.isoformat(),
        import_end.replace(hour=23, minute=59, second=59).isoformat(),
    )
    if import_result.get("success"):
        events = import_result.get("events") or []
        result["import_events_count"] = len(events)
        slots = calendar_events_to_blocked_slots(events)
        result["import_blocked_slots"] = slots[:8]

    if not result["preview_events"]:
        result["likely_causes"].append(
            f"No events in the next **{preview_days} days** on the signed-in account's default calendar."
        )
    else:
        result["likely_causes"].append(
            f"Found **{len(preview_result.get('events', []))}** event(s) in the next {preview_days} days on `/me/calendar`."
        )

    if import_result.get("success") and result["import_events_count"] == 0:
        result["likely_causes"].append(
            f"Timetable import scans **{import_days} days** — also empty in that range."
        )
    elif import_result.get("success"):
        result["likely_causes"].append(
            f"Timetable import window ({import_days} days) contains **{result['import_events_count']}** event(s)."
        )

    return result


def format_inbox_diagnostics_report(data: dict) -> str:
    if not data.get("ok") and data.get("error"):
        return f"**Inbox debug failed**\n\n{data['error']}"

    account = data.get("account") or {}
    folder = data.get("folder") or {}
    stats = data.get("stats") or {}
    token = data.get("token") or {}

    lines = [
        "**Inbox debug — what Graph sees**",
        "",
        "**Signed-in account**",
        f"- Name: {account.get('display_name') or token.get('name_from_token') or '(unknown)'}",
        f"- Mail: {account.get('mail') or '(not returned)'}",
        f"- UPN: {account.get('user_principal_name') or token.get('upn_from_token') or '(not returned)'}",
        "",
        "**Token**",
        f"- Scopes: {', '.join(token.get('scopes') or []) or '(not visible in token)'}",
        f"- Saved at: {token.get('saved_at') or '(unknown)'}",
        "",
        f"**Inbox folder ({folder.get('name') or 'Inbox'})**",
        f"- Total messages: {folder.get('total_item_count', '?')}",
        f"- Unread (Graph folder stat): {folder.get('unread_item_count', '?')}",
        "",
        "**Agent folders**",
    ]

    agent_folders = data.get("agent_folders") or []
    if agent_folders:
        for item in agent_folders:
            if item.get("exists"):
                lines.append(
                    f"- **{item.get('name')}**: {item.get('total', 0)} message(s) "
                    f"({item.get('unread', 0)} unread)"
                )
            else:
                lines.append(f"- **{item.get('name')}**: not created yet")
    else:
        lines.append("- (could not load agent folder stats)")

    lines.extend([
        "",
        "**Pipeline query**",
        f"- Filter: `{stats.get('pipeline_filter', '')}`",
        f"- Unread candidates fetched: **{stats.get('candidates_fetched', 0)}**",
        f"- Would classify on next inbox run: **{stats.get('would_process_now', 0)}**",
        f"- Archived content fingerprints stored: **{stats.get('archived_content_fingerprints', 0)}** "
        "(used for duplicate re-send detection, not message IDs)",
    ])

    candidate_sample = data.get("candidate_sample") or []
    if candidate_sample:
        lines.extend(["", "**Unread candidates (pipeline view)**"])
        for index, item in enumerate(candidate_sample[:8], start=1):
            lines.append(
                f"{index}. **{item.get('subject')}** — {item.get('from')} · {item.get('received')}"
            )
    else:
        lines.extend(["", "**Unread candidates:** none returned"])

    recent = data.get("recent_sample") or []
    if recent:
        lines.extend(["", "**Recent inbox (read + unread, top 8)**"])
        for index, item in enumerate(recent, start=1):
            read_label = "read" if item.get("is_read") else "unread"
            lines.append(
                f"{index}. [{read_label}] **{item.get('subject')}** — {item.get('from')} · {item.get('received')}"
            )

    causes = data.get("likely_causes") or []
    if causes:
        lines.extend(["", "**Likely explanation**"])
        for cause in causes:
            lines.append(f"- {cause}")

    return "\n".join(lines)


def format_calendar_diagnostics_report(data: dict) -> str:
    if not data.get("ok") and data.get("error"):
        return f"**Calendar debug failed**\n\n{data['error']}"

    preview = data.get("preview_range") or {}
    import_range = data.get("import_range") or {}
    lines = [
        "**Calendar debug — what Graph sees**",
        "",
        f"**Preview window:** next {preview.get('days', 14)} days",
        f"- From: `{preview.get('start', '')[:19]}`",
        f"- To: `{preview.get('end', '')[:19]}`",
        "",
        f"**Timetable import window:** {import_range.get('days', 120)} days (same as onboarding calendar prefill)",
        f"- Events in import range: **{data.get('import_events_count', 0)}**",
    ]

    events = data.get("preview_events") or []
    if events:
        lines.extend(["", "**Upcoming events (sample)**"])
        for index, event in enumerate(events, start=1):
            all_day = " (all day)" if event.get("all_day") else ""
            loc = f" @ {event.get('location')}" if event.get("location") else ""
            lines.append(
                f"{index}. **{event.get('subject')}** — {event.get('start')} → {event.get('end')}{all_day}{loc}"
            )
    else:
        lines.extend(["", "**Upcoming events:** none in preview window"])

    slots = data.get("import_blocked_slots") or []
    if slots:
        lines.extend(["", "**Blocked slots derived for timetable (sample)**"])
        for slot in slots:
            lines.append(
                f"- {slot.get('day')} {slot.get('start')}–{slot.get('end')}: {slot.get('label') or 'Busy'}"
            )

    causes = data.get("likely_causes") or []
    if causes:
        lines.extend(["", "**Likely explanation**"])
        for cause in causes:
            lines.append(f"- {cause}")

    return "\n".join(lines)
