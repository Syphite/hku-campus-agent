"""
agent/email_pipeline.py
Connects graph.py + classifier.py and returns inbox summary for digest.py
"""

import logging

from agent.classifier import classify_email
from agent.email_dedup import (
    append_fingerprints,
    check_duplicate,
    content_fingerprint,
    load_stored_fingerprints,
    preview_signature,
)
from agent.graph import (
    GraphApiError,
    archive_email,
    get_all_unread_emails,
    get_inbox_messages_since,
    hk_today_start_utc_iso,
    is_protected_sender,
    move_to_ambiguous_folder,
)
from agent.profile import save_profile

logger = logging.getLogger(__name__)


def fetch_inbox_candidates(user_token: str, profile: dict) -> tuple[list, str]:
    """
    Initial run: all unread (paginated).
    Later runs: messages received today (HKT) only.
    """
    if not profile.get("inbox_initial_scan_complete"):
        return get_all_unread_emails(user_token), "initial_unread_scan"
    since = hk_today_start_utc_iso()
    return get_inbox_messages_since(user_token, since), "today_received"


def run_inbox_pipeline(student_id: str, profile: dict = None, user_token: str | None = None) -> dict:
    """
    Reads inbox, classifies emails, routes to folders.
    Returns structured summary for digest.py.
    """
    profile = profile or {}
    if not user_token:
        raise GraphApiError(
            "Graph sign-in required",
            hint="Sign in with Microsoft to access your inbox.",
        )

    try:
        emails, scan_mode = fetch_inbox_candidates(user_token, profile)
    except GraphApiError as exc:
        logger.error(
            "Inbox pipeline Graph failure for student_id=%s: %s",
            student_id,
            exc.to_log_dict(),
        )
        raise

    processed_email_ids = profile.get("processed_email_ids", [])
    if not isinstance(processed_email_ids, list):
        processed_email_ids = []
    processed_set = {str(eid) for eid in processed_email_ids if eid}
    stored_fingerprints = load_stored_fingerprints(profile)

    archived_items = []
    relevant_items = []
    urgent_items = []
    ambiguous_items = []
    duplicate_items = []
    newly_processed_ids = []
    new_fingerprints: list[str] = []

    batch_fingerprints: set[str] = set()
    batch_previews: dict[str, str] = {}

    for email in emails:
        sender = email.get("from", {}).get("emailAddress", {}).get("address", "")
        subject = email.get("subject", "")
        preview = email.get("bodyPreview", "")
        eid = email.get("id", "")

        if not eid or eid in processed_set:
            continue

        fingerprint = content_fingerprint(sender, subject)
        is_dup, dup_reason = check_duplicate(
            fingerprint,
            preview,
            batch_fingerprints=batch_fingerprints,
            stored_fingerprints=stored_fingerprints,
            batch_previews=batch_previews,
        )
        batch_fingerprints.add(fingerprint)
        batch_previews[fingerprint] = preview_signature(preview)

        newly_processed_ids.append(eid)
        processed_set.add(eid)
        new_fingerprints.append(fingerprint)

        item = {
            "email_id": eid,
            "original_id": eid,
            "subject": subject,
            "from": sender,
            "reason": "",
            "body_preview": preview,
        }

        if is_dup:
            item["reason"] = dup_reason
            result = archive_email(eid, user_token)
            if result.get("success"):
                item["email_id"] = result.get("new_id") or eid
                archived_items.append(item)
                duplicate_items.append(item)
            else:
                ambiguous_items.append({**item, "reason": f"{dup_reason} (could not archive)"})
            continue

        if is_protected_sender(sender):
            item["reason"] = "Protected sender — always kept in inbox"
            urgent_items.append(item)
            continue

        result = classify_email(subject, preview, sender, profile)
        label = result["label"]
        item["reason"] = result["reason"]

        if label == "noise":
            move_result = archive_email(eid, user_token)
            if move_result.get("success"):
                item["email_id"] = move_result.get("new_id") or eid
                archived_items.append(item)
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

    if profile.get("id"):
        if newly_processed_ids:
            profile["processed_email_ids"] = processed_email_ids + newly_processed_ids
        append_fingerprints(profile, new_fingerprints)
        if newly_processed_ids or not profile.get("inbox_initial_scan_complete"):
            profile["inbox_initial_scan_complete"] = True
        save_profile(profile)

    processed_count = len(newly_processed_ids)
    kept_in_inbox = len(urgent_items) + len(relevant_items)
    return {
        "processed": processed_count,
        "archived": len(archived_items),
        "kept": kept_in_inbox,
        "ambiguous_moved": len(ambiguous_items),
        "scan_mode": scan_mode,
        "candidates_fetched": len(emails),
        "duplicates_archived": len(duplicate_items),
        "archived_items": archived_items,
        "relevant_items": relevant_items,
        "urgent_items": urgent_items,
        "ambiguous_items": ambiguous_items,
        "duplicate_items": duplicate_items,
    }
