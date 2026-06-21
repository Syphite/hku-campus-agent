"""
agent/email_pipeline.py
Connects graph.py + classifier.py and returns inbox summary for digest.py
"""

import logging

from agent.classifier import classify_email
from agent.email_dedup import (
    check_duplicate,
    content_fingerprint,
    load_archived_fingerprints,
    preview_signature,
    record_archived_fingerprint,
)
from agent.email_calendar import enrich_inbox_with_calendar
from agent.graph import (
    GraphApiError,
    archive_email,
    ensure_agent_mail_folders,
    get_all_unread_emails,
    is_protected_email,
    move_to_ambiguous_folder,
)
from agent.profile import save_profile

logger = logging.getLogger(__name__)

SCAN_MODE = "unread_scan"


def fetch_inbox_candidates(user_token: str, profile: dict) -> tuple[list, str]:
    """Always scan unread inbox messages (paginated)."""
    del profile  # kept for API compatibility with diagnostics
    return get_all_unread_emails(user_token), SCAN_MODE


def run_inbox_pipeline(student_id: str, profile: dict = None, user_token: str | None = None) -> dict:
    """
    Reads inbox, classifies emails, routes to folders.
    Dedup is content-based (sender + subject), not Graph message IDs.
    """
    profile = profile or {}
    if not user_token:
        raise GraphApiError(
            "Graph sign-in required",
            hint="Sign in with Microsoft to access your inbox.",
        )

    try:
        ensure_agent_mail_folders(user_token)
    except GraphApiError as exc:
        logger.error("Could not ensure agent mail folders: %s", exc.to_log_dict())
        raise

    try:
        emails, scan_mode = fetch_inbox_candidates(user_token, profile)
    except GraphApiError as exc:
        logger.error(
            "Inbox pipeline Graph failure for student_id=%s: %s",
            student_id,
            exc.to_log_dict(),
        )
        raise

    archived_fingerprints = load_archived_fingerprints(profile)

    archived_items = []
    relevant_items = []
    urgent_items = []
    ambiguous_items = []
    duplicate_items = []
    skipped_duplicates = 0

    batch_fingerprints: set[str] = set()
    batch_previews: dict[str, str] = {}

    for email in emails:
        sender = email.get("from", {}).get("emailAddress", {}).get("address", "")
        subject = email.get("subject", "")
        preview = email.get("bodyPreview", "")
        eid = email.get("id", "")

        if not eid:
            continue

        fingerprint = content_fingerprint(sender, subject)
        is_dup, dup_reason = check_duplicate(
            fingerprint,
            preview,
            batch_fingerprints=batch_fingerprints,
            archived_fingerprints=archived_fingerprints,
            batch_previews=batch_previews,
        )
        batch_fingerprints.add(fingerprint)
        batch_previews[fingerprint] = preview_signature(preview)

        item = {
            "email_id": eid,
            "original_id": eid,
            "subject": subject,
            "from": sender,
            "reason": "",
            "body_preview": preview,
            "web_link": email.get("webLink") or "",
        }

        if is_protected_email(sender, subject, preview):
            item["reason"] = "Protected sender or CEDARS message — kept in inbox"
            urgent_items.append(item)
            continue

        if is_dup:
            item["reason"] = dup_reason
            result = archive_email(eid, user_token)
            if result.get("success"):
                item["email_id"] = result.get("new_id") or eid
                archived_items.append(item)
                duplicate_items.append(item)
                record_archived_fingerprint(profile, fingerprint)
                archived_fingerprints.add(fingerprint)
            else:
                skipped_duplicates += 1
                ambiguous_items.append({**item, "reason": f"{dup_reason} (could not archive)"})
            continue

        result = classify_email(subject, preview, sender, profile)
        label = result["label"]
        item["reason"] = result["reason"]

        if label == "noise":
            move_result = archive_email(eid, user_token)
            if move_result.get("success"):
                item["email_id"] = move_result.get("new_id") or eid
                archived_items.append(item)
                record_archived_fingerprint(profile, fingerprint)
                archived_fingerprints.add(fingerprint)
            else:
                ambiguous_items.append({**item, "reason": "Classified as noise but could not archive."})

        elif label == "urgent":
            urgent_items.append(item)

        elif label == "relevant":
            relevant_items.append(item)

        elif label == "ambiguous":
            move_result = move_to_ambiguous_folder(eid, user_token)
            if move_result.get("success"):
                item["email_id"] = move_result.get("new_id") or eid
                ambiguous_items.append(item)
            else:
                ambiguous_items.append({**item, "reason": f"{item['reason']} (kept in inbox — could not move)"})

        else:
            ambiguous_items.append(item)

    enrich_inbox_with_calendar(urgent_items, relevant_items, profile, user_token)

    if profile.get("id"):
        save_profile(profile)

    processed_count = (
        len(archived_items)
        + len(urgent_items)
        + len(relevant_items)
        + len(ambiguous_items)
    )
    kept_in_inbox = len(urgent_items) + len(relevant_items)
    return {
        "processed": processed_count,
        "archived": len(archived_items),
        "kept": kept_in_inbox,
        "ambiguous_moved": len(ambiguous_items),
        "scan_mode": scan_mode,
        "candidates_fetched": len(emails),
        "duplicates_archived": len(duplicate_items),
        "skipped_duplicates": skipped_duplicates,
        "archived_items": archived_items,
        "relevant_items": relevant_items,
        "urgent_items": urgent_items,
        "ambiguous_items": ambiguous_items,
        "duplicate_items": duplicate_items,
    }
