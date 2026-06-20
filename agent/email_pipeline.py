"""
agent/email_pipeline.py
Connects graph.py + classifier.py and returns inbox summary for digest.py
"""

from agent.graph      import get_unread_emails, archive_email, is_protected_sender, USER_EMAIL
from agent.classifier import classify_email
from agent.profile    import save_profile


def run_inbox_pipeline(student_id: str, profile: dict = None) -> dict:
    """
    Reads inbox, classifies emails, archives noise.
    Returns structured summary for digest.py.
    """
    profile = profile or {}
    user_email = USER_EMAIL or profile.get("email")
    emails  = get_unread_emails(user_email)
    processed_email_ids = profile.get("processed_email_ids", [])
    if not isinstance(processed_email_ids, list):
        processed_email_ids = []
    processed_set = {str(eid) for eid in processed_email_ids if eid}

    archived_items  = []
    relevant_items  = []
    urgent_items    = []
    ambiguous_items = []
    newly_processed_ids = []

    for email in emails:
        sender  = email.get("from", {}).get("emailAddress", {}).get("address", "")
        subject = email.get("subject", "")
        preview = email.get("bodyPreview", "")
        eid     = email.get("id", "")

        if not eid or eid in processed_set:
            continue

        newly_processed_ids.append(eid)
        processed_set.add(eid)

        # Never archive protected senders
        if is_protected_sender(sender):
            urgent_items.append({
                "email_id": eid,
                "original_id": eid,
                "subject":  subject,
                "from":     sender,
                "reason":   "Protected sender — always kept in inbox",
                "body_preview": preview
            })
            continue

        result = classify_email(subject, preview, sender, profile)
        label  = result["label"]
        reason = result["reason"]

        item = {
            "email_id": eid,
            "original_id": eid,
            "subject": subject,
            "from": sender,
            "reason": reason,
            "body_preview": preview
        }

        if label == "noise":
            result = archive_email(eid, user_email)
            if result.get("success"):
                item["email_id"] = result.get("new_id") or eid
                item["original_id"] = eid
                archived_items.append(item)
            else:
                ambiguous_items.append(item)  # failed to archive, keep visible

        elif label == "urgent":
            urgent_items.append(item)

        elif label == "relevant":
            relevant_items.append(item)

        else:  # ambiguous
            ambiguous_items.append(item)

    if newly_processed_ids and profile.get("id"):
        profile["processed_email_ids"] = processed_email_ids + newly_processed_ids
        save_profile(profile)

    processed_count = len(newly_processed_ids)
    return {
        "processed":       processed_count,
        "archived":        len(archived_items),
        "kept":            processed_count - len(archived_items),
        "archived_items":  archived_items,
        "relevant_items":  relevant_items,
        "urgent_items":    urgent_items,
        "ambiguous_items": ambiguous_items,
    }
