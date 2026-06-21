"""Proactive briefing and session snapshot for the campus agent."""

from __future__ import annotations

from datetime import datetime, timezone


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def _humanize_delta(last_seen: datetime | None) -> str:
    if not last_seen:
        return "your first visit"
    now = datetime.now(timezone.utc)
    delta = now - last_seen
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        return "a few minutes ago"
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    return last_seen.strftime("%d %b")


def _snapshot_counts(digest: dict) -> dict:
    summary = digest.get("summary") or {}
    inbox = digest.get("inbox") or {}
    scholarships = digest.get("scholarships") or {}
    events = digest.get("events") or {}
    apply_now = scholarships.get("apply_now") or []
    return {
        "scholarships_open": int(summary.get("scholarships_open") or 0),
        "scholarships_prepare": int(summary.get("scholarships_prepare") or 0),
        "events_urgent": int(summary.get("events_urgent") or 0),
        "events_upcoming": int(summary.get("events_upcoming") or 0),
        "emails_processed": int(summary.get("emails_processed") or inbox.get("processed") or 0),
        "emails_archived": int(summary.get("emails_archived") or inbox.get("archived") or 0),
        "emails_kept": int(inbox.get("kept") or 0),
        "top_scholarship_names": [
            str(item.get("name") or "").strip()
            for item in apply_now[:3]
            if str(item.get("name") or "").strip()
        ],
    }


def build_suggested_actions(digest: dict, profile: dict) -> list[str]:
    """Short proactive next-step suggestions based on digest content."""
    actions: list[str] = []
    scholarships = digest.get("scholarships") or {}
    events = digest.get("events") or {}
    inbox = digest.get("inbox") or {}

    apply_now = scholarships.get("apply_now") or []
    if apply_now:
        name = str(apply_now[0].get("name") or "a scholarship").strip()
        actions.append(f"Review **{name}** — deadline approaching.")

    urgent_events = events.get("urgent") or []
    if urgent_events:
        title = str(urgent_events[0].get("title") or urgent_events[0].get("name") or "an event").strip()
        actions.append(f"Check **{title}** before the deadline passes.")

    if (inbox.get("processed") or 0) > 0 and (inbox.get("archived_items") or []):
        actions.append("Review one archived email to confirm the triage was correct.")

    gpa = profile.get("academic", {}).get("gpa") or 0
    try:
        gpa_ok = float(gpa) >= 3.5
    except (TypeError, ValueError):
        gpa_ok = False
    if gpa_ok and not any("chen" in (n or "").lower() for n in _snapshot_counts(digest).get("top_scholarship_names", [])):
        actions.append("Say **apply to Chen scholarship** to start the D. H. Chen Foundation application.")

    if not actions:
        actions.append("Update your interests if your focus has changed — I'll refine matches automatically.")
    return actions[:3]


def build_proactive_intro(profile: dict, digest: dict) -> str:
    """Opening briefing comparing current digest to the last session snapshot."""
    name = str(profile.get("name") or "there").split()[0] or "there"
    snapshot = profile.get("agent_snapshot") or {}
    last_seen = _parse_iso(snapshot.get("last_seen_at"))
    previous = snapshot.get("counts") or {}
    current = _snapshot_counts(digest)

    lines = [f"👋 **Welcome back, {name}** — last seen {_humanize_delta(last_seen)}.\n"]

    deltas: list[str] = []
    for key, label in (
        ("scholarships_open", "open scholarship(s)"),
        ("scholarships_prepare", "scholarship(s) to prepare for"),
        ("events_urgent", "urgent event(s)"),
        ("emails_processed", "email(s) processed"),
    ):
        prev_val = int(previous.get(key) or 0)
        curr_val = int(current.get(key) or 0)
        diff = curr_val - prev_val
        if diff > 0 and not last_seen:
            deltas.append(f"• **{curr_val}** {label} on this run")
        elif diff > 0:
            deltas.append(f"• **+{diff}** {label} since last visit")
        elif curr_val > 0 and not last_seen:
            deltas.append(f"• **{curr_val}** {label}")

    if deltas:
        lines.append("**What I've picked up:**")
        lines.extend(deltas)
        lines.append("")

    actions = build_suggested_actions(digest, profile)
    if actions:
        lines.append("**Suggested next steps:**")
        for action in actions:
            lines.append(f"• {action}")
        lines.append("")

    return "\n".join(lines).strip()


def update_agent_snapshot(profile: dict, digest: dict) -> None:
    """Persist latest digest counts for proactive delta messaging."""
    now = datetime.now(timezone.utc).isoformat()
    profile["agent_snapshot"] = {
        "last_seen_at": now,
        "last_digest_at": digest.get("generated_at") or now,
        "counts": _snapshot_counts(digest),
    }


def format_digest_with_proactive(profile: dict, digest: dict, base_message: str) -> str:
    """Combine proactive intro with the standard digest summary."""
    intro = build_proactive_intro(profile, digest)
    if not base_message:
        return intro
    return f"{intro}\n\n---\n\n{base_message}"
