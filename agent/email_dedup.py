"""Duplicate detection for inbox triage (content-based, not message IDs)."""

from __future__ import annotations

import re

_DATE_IN_SUBJECT = re.compile(
    r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b"
)
_PREFIX_RE = re.compile(r"^(re|fw|fwd):\s*", re.IGNORECASE)


def normalize_subject(subject: str) -> str:
    text = (subject or "").strip().lower()
    while True:
        updated = _PREFIX_RE.sub("", text).strip()
        if updated == text:
            break
        text = updated
    text = _DATE_IN_SUBJECT.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def content_fingerprint(sender: str, subject: str) -> str:
    return f"{(sender or '').strip().lower()}|{normalize_subject(subject)}"


def preview_signature(preview: str, limit: int = 120) -> str:
    return re.sub(r"\s+", " ", (preview or "").strip().lower())[:limit]


def check_duplicate(
    fingerprint: str,
    preview: str,
    *,
    batch_fingerprints: set[str],
    archived_fingerprints: set[str],
    batch_previews: dict[str, str],
) -> tuple[bool, str]:
    """True when this looks like the same content as mail we already archived."""
    if fingerprint in batch_fingerprints:
        return True, "Duplicate in this run (same sender and subject)."
    if fingerprint in archived_fingerprints:
        return True, "Duplicate re-send (same sender and subject as mail already archived)."
    prior_preview = batch_previews.get(fingerprint)
    sig = preview_signature(preview)
    if prior_preview and sig and prior_preview == sig:
        return True, "Duplicate in this run (same sender, subject, and preview)."
    return False


def load_archived_fingerprints(profile: dict) -> set[str]:
    raw = profile.get("inbox_content_fingerprints") or []
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw if item}


def record_archived_fingerprint(profile: dict, fingerprint: str, max_stored: int = 200) -> None:
    """Remember content we archived so duplicate re-sends can be collapsed later."""
    if not fingerprint:
        return
    existing = list(profile.get("inbox_content_fingerprints") or [])
    if not isinstance(existing, list):
        existing = []
    if fingerprint not in existing:
        existing.append(fingerprint)
    profile["inbox_content_fingerprints"] = existing[-max_stored:]


# Backwards-compatible aliases
load_stored_fingerprints = load_archived_fingerprints


def append_fingerprints(profile: dict, fingerprints: list[str], max_stored: int = 200) -> None:
    for fp in fingerprints:
        record_archived_fingerprint(profile, fp, max_stored=max_stored)
