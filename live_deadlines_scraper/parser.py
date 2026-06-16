"""
live_deadlines_scraper/parser.py
Scrapes https://aas.hku.hk/apply-scholarships/ for open scholarships with deadlines.
No Azure needed. Run: python3 parser.py
"""

import re, logging, json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
import requests
from bs4 import BeautifulSoup

logger   = logging.getLogger(__name__)
LIVE_URL = "https://aas.hku.hk/apply-scholarships/"
HEADERS  = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}


@dataclass
class LiveScholarship:
    name: str
    deadline_raw: str
    deadline_iso: Optional[str]
    is_rolling: bool
    application_url: str
    application_method: str
    form_url: Optional[str]
    source: str
    ss_id: Optional[int]
    scholar_url: Optional[str]
    level: str
    page_last_updated: str
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def get_soup(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def parse_deadline_iso(raw: str) -> Optional[str]:
    clean = re.sub(r"\d{1,2}:\d{2}\s*(noon|am|pm)?", "", raw, flags=re.IGNORECASE)
    clean = re.sub(r"\(.*?\)", "", clean)
    clean = re.sub(r"before|by|at|,", " ", clean, flags=re.IGNORECASE).strip()
    for fmt in ["%B %d %Y", "%d %B %Y", "%B %Y"]:
        try:
            return datetime.strptime(clean.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def extract_ss_id(url: str) -> Optional[int]:
    m = re.search(r"ss_id=(\d+)", url)
    return int(m.group(1)) if m else None


def extract_form_url(cell_html: str) -> Optional[str]:
    soup = BeautifulSoup(cell_html, "html.parser")
    for a in soup.find_all("a", href=True):
        if any(a["href"].lower().endswith(ext) for ext in [".doc",".docx",".pdf"]):
            return a["href"]
    return None


def parse_table(table, source: str, level: str, updated: str) -> list:
    results = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2: continue
        link = cells[0].find("a")
        if not link: continue

        name = link.get_text(strip=True)
        href = link.get("href","")
        scholar_url = href if "scholar.aas.hku.hk" in href else None
        ss_id       = extract_ss_id(href) if scholar_url else None
        app_url     = href

        dl_raw    = cells[1].get_text(separator=" ", strip=True)
        is_roll   = bool(re.search(r"rolling|no fixed|not specified", dl_raw, re.IGNORECASE))
        dl_iso    = None if is_roll else parse_deadline_iso(dl_raw)

        method = form_url = ""
        if len(cells) >= 3:
            method   = cells[2].get_text(separator=" ", strip=True)
            form_url = extract_form_url(str(cells[2]))
            m = re.search(r"\(HKU Portal[^)]+\)", method)
            if m: method = m.group(0).strip("()")

        if source == "external" and href.startswith("http"):
            app_url = href

        results.append(LiveScholarship(
            name=name, deadline_raw=dl_raw, deadline_iso=dl_iso,
            is_rolling=is_roll, application_url=app_url,
            application_method=method, form_url=form_url,
            source=source, ss_id=ss_id, scholar_url=scholar_url,
            level=level, page_last_updated=updated,
        ))
    return results


def scrape_live_deadlines() -> dict:
    soup      = get_soup(LIVE_URL)
    full_text = soup.get_text()
    um        = re.search(r"Last updated on\s+(\d{4}-\d{2}-\d{2})", full_text)
    updated   = um.group(1) if um else ""

    hku_open = []; ext_open = []
    content  = soup.find("div", class_="entry-content") or soup.find("article") or soup
    src = "hku"; lvl = "undergraduate"

    for el in content.descendants:
        if el.name == "h2":
            t = el.get_text(strip=True).lower()
            if "external"    in t: src = "external"
            elif "hku" in t or "administered" in t: src = "hku"
        elif el.name == "table":
            fr = el.find("tr")
            if fr:
                ft = fr.get_text(strip=True).lower()
                if "postgraduate"  in ft: lvl = "postgraduate"
                elif "undergraduate" in ft: lvl = "undergraduate"
            recs = parse_table(el, src, lvl, updated)
            (hku_open if src == "hku" else ext_open).extend(recs)

    logger.info(f"Live: {len(hku_open)} HKU open, {len(ext_open)} external open (updated {updated})")
    return {
        "hku_open":         [asdict(s) for s in hku_open],
        "external_open":    [asdict(s) for s in ext_open],
        "page_last_updated": updated,
        "scraped_at":       datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = scrape_live_deadlines()
    print(json.dumps(result, indent=2))
