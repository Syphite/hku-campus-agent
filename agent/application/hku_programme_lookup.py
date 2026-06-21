"""Look up normative study duration for HKU programmes."""

from __future__ import annotations

import logging
import re
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

# Normative duration in years for common HKU undergraduate programmes.
HKU_PROGRAMME_DURATION: dict[str, int] = {
    "beng(cs)": 4,
    "beng (cs)": 4,
    "bengcse": 4,
    "bachelor of engineering in computer science": 4,
    "bsc(cs)": 4,
    "bachelor of science in computer science": 4,
    "bba": 4,
    "bachelor of business administration": 4,
    "ba": 4,
    "bachelor of arts": 4,
    "bsc": 4,
    "bachelor of science": 4,
    "beng": 4,
    "bachelor of engineering": 4,
    "mbbs": 6,
    "bachelor of medicine and bachelor of surgery": 6,
    "llb": 4,
    "bachelor of laws": 4,
    "barch": 5,
    "bachelor of architecture": 5,
    "bed": 5,
    "bachelor of education": 5,
}

_DURATION_PATTERNS = (
    re.compile(r"normative\s+(?:period|duration)\s+(?:of\s+study\s*)?[:\s]*(\d+)\s*years?", re.I),
    re.compile(r"(\d+)\s*-?\s*year\s+(?:full[- ]time\s+)?(?:undergraduate|programme|degree)", re.I),
    re.compile(r"duration\s+(?:of\s+study\s*)?[:\s]*(\d+)\s*years?", re.I),
)


def _normalize_programme_key(programme: str) -> str:
    return re.sub(r"\s+", " ", str(programme or "").strip().lower())


def _lookup_curated(programme: str) -> str:
    key = _normalize_programme_key(programme)
    if not key:
        return ""
    if key in HKU_PROGRAMME_DURATION:
        years = HKU_PROGRAMME_DURATION[key]
        return f"{years} years"
    for pattern, years in HKU_PROGRAMME_DURATION.items():
        if pattern in key or key in pattern:
            return f"{years} years"
    if "master" in key or "mphil" in key or "postgraduate diploma" in key:
        return "1-2 years"
    if "doctor" in key or "phd" in key:
        return "3-4 years"
    if "bachelor" in key or key.startswith("b"):
        return "4 years"
    return ""


def _extract_duration_from_html(html: str) -> str:
    if not html:
        return ""
    for pattern in _DURATION_PATTERNS:
        match = pattern.search(html)
        if match:
            return f"{match.group(1)} years"
    return ""


def _fetch_hku_duration(programme: str) -> str:
    """Best-effort web lookup via HKU search results."""
    try:
        import urllib.request

        query = quote_plus(f"site:hku.hk {programme} normative period of study")
        url = f"https://www.google.com/search?q={query}"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "HKU-Campus-Agent/1.0"},
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            html = response.read().decode("utf-8", errors="ignore")
        return _extract_duration_from_html(html)
    except Exception as exc:
        logger.debug("HKU programme web lookup failed for %s: %s", programme, exc)
        return ""


def lookup_normative_duration(programme: str) -> str:
    """
    Return normative study duration for an HKU programme (e.g. "4 years").
    Uses a curated map first, then a lightweight web lookup fallback.
    """
    curated = _lookup_curated(programme)
    if curated:
        return curated
    return _fetch_hku_duration(programme)
