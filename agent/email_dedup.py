"""Duplicate detection for inbox triage."""

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
    stored_fingerprints: set[str],
    batch_previews: dict[str, str],
) -> tuple[bool, str]:
    if fingerprint in batch_fingerprints:
        return True, "Duplicate in this run (same sender and subject)."
    if fingerprint in stored_fingerprints:
        return True, "Duplicate re-send (same sender and subject as a prior message)."
    prior_preview = batch_previews.get(fingerprint)
    sig = preview_signature(preview)
    if prior_preview and sig and prior_preview == sig:
        return True, "Duplicate in this run (same sender, subject, and preview)."
    return False, ""


def load_stored_fingerprints(profile: dict) -> set[str]:
    raw = profile.get("inbox_content_fingerprints") or []
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw if item}


def append_fingerprints(profile: dict, fingerprints: list[str], max_stored: int = 200) -> None:
    existing = list(profile.get("inbox_content_fingerprints") or [])
    if not isinstance(existing, list):
        existing = []
    for fp in fingerprints:
        if fp and fp not in existing:
            existing.append(fp)
    profile["inbox_content_fingerprints"] = existing[-max_stored:]
