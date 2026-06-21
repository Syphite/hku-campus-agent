"""Normalize and validate datetimes for Graph calendar API calls."""

from __future__ import annotations

import re
from datetime import datetime

_ISO_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$"
)


def normalize_time_hhmm(value: str) -> str | None:
    """Return HH:MM from a time string or ISO datetime fragment."""
    raw = str(value or "").strip()
    if not raw:
        return None

    if "T" in raw:
        try:
            normalized = raw.split(".")[0].replace("Z", "")
            dt = datetime.fromisoformat(normalized)
            return f"{dt.hour:02d}:{dt.minute:02d}"
        except ValueError:
            pass

    match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", raw)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"

    last_valid = None
    for candidate in re.finditer(r"(\d{1,2}):(\d{2})", raw):
        hour, minute = int(candidate.group(1)), int(candidate.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            last_valid = f"{hour:02d}:{minute:02d}"
    return last_valid


def combine_date_and_time(date_iso: str, time_hhmm: str) -> str:
    """Build Graph-friendly local datetime YYYY-MM-DDTHH:MM:SS."""
    date_part = str(date_iso or "").strip()[:10]
    time_part = normalize_time_hhmm(time_hhmm) or "09:00"
    return f"{date_part}T{time_part}:00"


def validate_iso_datetime(value: str) -> bool:
    """Reject malformed Graph dateTime strings before API calls."""
    text = str(value or "").strip()
    if not _ISO_DATETIME.match(text):
        return False
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True
