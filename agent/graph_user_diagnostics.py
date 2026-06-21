"""
Delegated Graph diagnostics for the signed-in Copilot user.
Used by debug inbox / debug calendar chat commands.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent.email_dedup import load_stored_fingerprints
from agent.email_pipeline import fetch_inbox_candidates
from agent.graph import (
    GraphApiError,
    calendar_events_to_blocked_slots,
    get_calendar_events,
    get_inbox_folder_stats,
    get_inbox_messages,
    get_me_profile,
    hk_today_start_utc_iso,
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
    if scan_mode == "initial_unread_scan":
        return "isRead eq false, paginated (initial scan, max 500)"
    return f"receivedDateTime ge {hk_today_start_utc_iso()} (today HKT, max 100)"


def _inbox_likely_causes(stats: dict) -> list[str]:
    causes = []
    candidates = int(stats.get("candidates_fetched") or 0)
    skipped_processed = int(stats.get("skipped_already_processed") or 0)
    scan_mode = stats.get("scan_mode") or ""
    folder_unread = stats.get("folder_unread")
    folder_total = stats.get("folder_total")
    initial_complete = stats.get("inbox_initial_scan_complete")

    if scan_mode == "initial_unread_scan":
        if folder_unread == 0 and folder_total and folder_total > 0:
            causes.append("Your inbox has messages, but **none are unread**. The first scan only processes unread mail.")
        elif folder_unread == 0 and folder_total == 0:
            causes.append("Graph returned an **empty inbox folder** for this signed-in account.")
    else:
        if candidates == 0:
            causes.append("No messages **received today (HKT)** in inbox. Daily runs only look at today's mail after the first scan.")

    if candidates > 0 and skipped_processed == candidates:
        causes.append(
            f"All **{candidates}** candidate message(s) were already in your processed list from a prior run."
        )
    elif candidates > 0 and skipped_processed > 0:
        causes.append(
            f"**{skipped_processed}** candidate message(s) were skipped as already processed; only new IDs are classified."
        )

    if not initial_complete:
        causes.append("**First inbox scan not complete** — next run will paginate all unread messages.")

    if not causes:
        causes.append("Graph is returning mail for this account. If counts are still zero, wait for new mail or run **inbox** again.")
    return causes


def run_inbox_diagnostics(user_token: str, profile: dict) -> dict:
    """Inspect mailbox identity, folder counts, and pipeline visibility."""
    processed_ids = profile.get("processed_email_ids") or []
    if not isinstance(processed_ids, list):
        processed_ids = []
    processed_set = {str(eid) for eid in processed_ids if eid}
    stored_fingerprints = load_stored_fingerprints(profile)
    initial_complete = bool(profile.get("inbox_initial_scan_complete"))

    result = {
        "ok": True,
        "error": "",
        "account": {},
        "token": _token_summary(user_token, profile),
        "folder": {},
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

    candidates = []
    scan_mode = ""
    try:
        candidates, scan_mode = fetch_inbox_candidates(user_token, profile)
    except GraphApiError as exc:
        result["ok"] = False
        result["error"] = result["error"] or f"Inbox fetch failed ({exc.status}): {exc.error_code or exc.error_message}"
        return result

    skipped = 0
    for email in candidates:
        eid = str(email.get("id") or "")
        if eid in processed_set:
            skipped += 1
        result["candidate_sample"].append({
            "id": eid[:24] + "…" if len(eid) > 24 else eid,
            "subject": _shorten(email.get("subject", "")),
            "from": _sender_address(email),
            "received": (email.get("receivedDateTime") or "")[:19],
            "is_read": bool(email.get("isRead")),
            "already_processed": eid in processed_set,
            "preview": _shorten(email.get("bodyPreview", ""), 100),
        })

    try:
        recent = get_inbox_messages(user_token, top=8, unread_only=False)
        for email in recent:
            eid = str(email.get("id") or "")
            result["recent_sample"].append({
                "subject": _shorten(email.get("subject", "")),
                "from": _sender_address(email),
                "received": (email.get("receivedDateTime") or "")[:19],
                "is_read": bool(email.get("isRead")),
                "already_processed": eid in processed_set,
            })
    except GraphApiError:
        pass

    result["stats"] = {
        "processed_ids_stored": len(processed_set),
        "content_fingerprints_stored": len(stored_fingerprints),
        "inbox_initial_scan_complete": initial_complete,
        "scan_mode": scan_mode,
        "candidates_fetched": len(candidates),
        "skipped_already_processed": skipped,
        "would_process_now": len(candidates) - skipped,
        "folder_total": result["folder"].get("total_item_count"),
        "folder_unread": result["folder"].get("unread_item_count"),
        "pipeline_filter": _pipeline_filter_description(scan_mode),
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
    scan_mode = stats.get("scan_mode") or ""
    mode_label = "first-time unread scan" if scan_mode == "initial_unread_scan" else "today's mail (HKT)"

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
        "**Pipeline query**",
        f"- Scan mode: **{mode_label}**",
        f"- Initial scan complete: **{stats.get('inbox_initial_scan_complete', False)}**",
        f"- Filter: `{stats.get('pipeline_filter', '')}`",
        f"- Candidates fetched: **{stats.get('candidates_fetched', 0)}**",
        f"- Already processed (skipped): **{stats.get('skipped_already_processed', 0)}**",
        f"- Would process on next inbox run: **{stats.get('would_process_now', 0)}**",
        f"- Processed IDs stored on profile: **{stats.get('processed_ids_stored', 0)}**",
        f"- Content fingerprints stored: **{stats.get('content_fingerprints_stored', 0)}**",
    ]

    candidate_sample = data.get("candidate_sample") or []
    sample_title = "Candidate messages (pipeline view)"
    if candidate_sample:
        lines.extend(["", f"**{sample_title}**"])
        for index, item in enumerate(candidate_sample[:8], start=1):
            flags = []
            if item.get("already_processed"):
                flags.append("already processed")
            if item.get("is_read"):
                flags.append("marked read")
            flag_text = f" ({', '.join(flags)})" if flags else ""
            lines.append(
                f"{index}. **{item.get('subject')}** — {item.get('from')} · {item.get('received')}{flag_text}"
            )
    else:
        lines.extend(["", f"**{sample_title}:** none returned"])

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
