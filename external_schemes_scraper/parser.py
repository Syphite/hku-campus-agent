"""
external_schemes_scraper/parser.py
Scrapes https://aas.hku.hk/external-schemes/ weekly.
"""

import re, logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

logger       = logging.getLogger(__name__)
EXTERNAL_URL = "https://aas.hku.hk/external-schemes/"
HEADERS      = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}


@dataclass
class ExternalScholarship:
    id: str
    name: str
    source: str = "external_scheme"
    source_url: str = EXTERNAL_URL
    scholarship_url: str = ""
    local_or_overseas: str = ""
    category: str = ""
    level: list = field(default_factory=list)
    target_students: list = field(default_factory=list)
    contact_info: str = ""
    is_open: bool = False
    deadline_raw: str = "See scholarship website"
    eligibility_raw: str = ""
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def stable_id(name: str) -> str:
    clean = re.sub(r"[^a-z0-9]", "_", name.lower())[:40]
    return f"ext_{clean}_{abs(hash(name)) % 10000}"


def infer_level(cat: str) -> list:
    c = cat.lower()
    out = []
    if "undergraduate" in c: out.append("undergraduate")
    if "postgraduate" in c or "doctoral" in c or "phd" in c: out.append("postgraduate")
    return out or ["all"]


def infer_targets(cat: str) -> list:
    c = cat.lower()
    t = []
    if "prospective" in c: t.append("prospective")
    if "current"     in c: t.append("current")
    if "special needs" in c or "disabled" in c: t.append("special_needs")
    if "ethnic minority" in c: t.append("ethnic_minority")
    return t or ["current"]


def parse_external_schemes() -> list:
    resp  = requests.get(EXTERNAL_URL, headers=HEADERS, timeout=15)
    soup  = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if not table:
        logger.error("No table found on external schemes page")
        return []

    results = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 3: continue
        lo  = cells[0].get_text(strip=True)
        cat = cells[1].get_text(strip=True)
        lnk = cells[2].find("a")
        if not lnk: continue
        name    = lnk.get_text(strip=True)
        url     = lnk.get("href", "")
        contact = cells[3].get_text(separator=" ", strip=True) if len(cells) >= 4 else ""
        elig    = f"Local or Overseas: {lo}. Eligible: {cat}. Contact: {contact}"
        results.append(asdict(ExternalScholarship(
            id=stable_id(name), name=name, scholarship_url=url,
            local_or_overseas=lo, category=cat,
            level=infer_level(cat), target_students=infer_targets(cat),
            contact_info=contact, eligibility_raw=elig,
        )))

    logger.info(f"Parsed {len(results)} external scholarships")
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    results = parse_external_schemes()
    print(f"\nParsed {len(results)} external scholarships:\n")
    for s in results:
        print(f"  {s['name'][:55]:<55} | {s['local_or_overseas']:<20} | level={s['level']}")
