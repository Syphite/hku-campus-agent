"""
scholarship_scraper/parser.py
Scrapes https://scholar.aas.hku.hk and returns structured scholarship dicts.
"""

import re
import time
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL     = "https://scholar.aas.hku.hk"
CURRENT_YEAR = datetime.now(timezone.utc).year


@dataclass
class Scholarship:
    id: str
    ss_id: int
    name: str
    source_url: str
    provider: str
    amount: str
    currency: str
    value_raw: str
    faculty: list
    level: list
    year_of_study: list
    nationality: list
    gpa_requirement: Optional[float]
    financial_need: bool
    merit_based: bool
    is_entrance: bool
    is_enrichment: bool
    submission_materials: list
    deadline_raw: str
    deadline_confidence: str
    application_url: str
    renewable: bool
    renewal_conditions: str
    duration: str
    eligibility_raw: str
    place_of_origin: str
    last_updated: str
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def get_soup(url: str, retries: int = 3, method: str = "GET", data: dict = None) -> BeautifulSoup:
    """Fetches URL and returns BeautifulSoup object. Supports GET and POST."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    for attempt in range(retries):
        try:
            if method.upper() == "POST":
                resp = requests.post(url, headers=headers, data=data, timeout=15)
            else:
                resp = requests.get(url, headers=headers, timeout=15)
                
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as e:
            logger.warning(f"Attempt {attempt+1} failed for {url}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts")


def get_listing_ids() -> list:
    """Scrape all scholarship IDs by paginating through the 'Show All' index via POST."""
    all_ids = []
    seen = set()

    # The site requires a POST request to paginate. 
    # We know the 'Show All' view spans exactly 14 pages.
    for page_num in range(1, 15):
        payload = {
            'page': str(page_num),
            'index': 'Show All'
        }
        try:
            # The form action targets index.php
            url = f"{BASE_URL}/index.php"
            soup = get_soup(url, method="POST", data=payload)
            
            # Extract IDs from the table links
            for a in soup.select("table a[href*='showonesscheme']"):
                match = re.search(r"ss_id=(\d+)", a.get("href", ""))
                if match:
                    ss_id = int(match.group(1))
                    if ss_id not in seen:
                        seen.add(ss_id)
                        all_ids.append(ss_id)
                        
            logger.info(f"Page {page_num}: total IDs so far: {len(all_ids)}")
            time.sleep(0.5) # Be polite to the server
        except Exception as e:
            logger.error(f"Failed to fetch page {page_num}: {e}")

    logger.info(f"Found {len(all_ids)} total unique scholarships")
    return all_ids


def parse_faculty(s: str) -> list:
    if not s or s.strip().lower() in ("not specified", ""):
        return ["all"]
    return [f.strip() for f in s.split(",") if f.strip()]


def parse_level(s: str) -> list:
    s = s.lower()
    out = []
    if "undergraduate" in s: out.append("undergraduate")
    if "postgraduate"  in s: out.append("postgraduate")
    return out or ["all"]


def parse_nationality(text: str, origin: str) -> list:
    c = (origin + " " + text).lower()
    if "local" in c and "non-local" in c: return ["all"]
    if "non-local" in c: return ["non-local"]
    if "local"     in c: return ["local"]
    return ["all"]


def parse_year(text: str, types: list) -> list:
    if "entrance" in types:
        return ["new_student"]
    years = set()
    tl = text.lower()
    for p in [r"year\s+([1-4])\b", r"\b([1-4])(?:st|nd|rd|th)\s+year"]:
        for m in re.finditer(p, tl):
            for g in m.groups():
                if g: years.add(g)
    if "non-final year" in tl: years.update(["1","2","3"])
    if "penultimate"    in tl: years.add("penultimate")
    if "final year"     in tl and "non-final" not in tl: years.add("final")
    return sorted(years) if years else ["all"]


def parse_provider(name: str) -> str:
    nl = name.lower()
    if nl.startswith("hku") or "university of hong kong" in nl:
        return "HKU"
    for s in [" scholarship"," fellowship"," award"," fund"," bursary"]:
        if nl.endswith(s): return name[:-len(s)].strip()
    return name.split("-")[0].strip()


def parse_amount(value_raw: str):
    if not value_raw or value_raw.lower().startswith("please see"):
        return "See scholarship page", "HKD"
    cur = "HKD"
    if "us$" in value_raw.lower() or "usd" in value_raw.lower(): cur = "USD"
    elif "rmb" in value_raw.lower() or "cny" in value_raw.lower(): cur = "CNY"
    elif "£" in value_raw: cur = "GBP"
    amount = re.sub(r"HK\$|US\$|RMB|£|\$", "", value_raw).strip()
    return amount, cur


def extract_gpa(text: str) -> Optional[float]:
    for p in [
        r"[Gg][Pp][Aa]\s+(?:of\s+)?(\d+\.\d+)",
        r"[Gg][Pp][Aa]\s+(?:at\s+)?(\d+\.\d+)",
        r"(\d+\.\d+)\s*\(or equivalent\)",
        r"cumulative\s+[Gg][Pp][Aa]\s+of\s+(\d+\.\d+)",
    ]:
        m = re.search(p, text)
        if m:
            val = float(m.group(1))
            if 0.0 <= val <= 4.3: return val
    return None


def extract_deadline(text: str):
    clean = re.sub(r"last update[d]?\s*:.*", "", text, flags=re.IGNORECASE)
    sentences = re.split(r"[.\n]", clean)

    app_kw  = ["application","apply","open","submit","deadline","accepting","close","window","invitation"]
    excl_kw = ["awardee","attach","result","supported","trip","travel","between","award period","supported to"]

    patterns = [
        (r"(late[- ](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*)",  "high"),
        (r"(early[- ](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*)", "high"),
        (r"(mid[- ](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*)",   "high"),
        (r"((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})", "low"),
        (r"(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})", "low"),
    ]

    def future_ok(s: str) -> bool:
        m = re.search(r"\d{4}", s)
        return True if not m else int(m.group()) >= CURRENT_YEAR - 1

    for sentence in sentences:
        sl = sentence.lower()
        if any(k in sl for k in app_kw) and not any(k in sl for k in excl_kw):
            for pat, conf in patterns:
                m = re.search(pat, sentence, re.IGNORECASE)
                if m and future_ok(m.group(1)):
                    return m.group(1).strip(), conf

    for sentence in sentences:
        sl = sentence.lower()
        if any(k in sl for k in app_kw):
            for pat, conf in patterns:
                m = re.search(pat, sentence, re.IGNORECASE)
                if m and future_ok(m.group(1)):
                    return m.group(1).strip(), "low"

    return "See scholarship page for deadline", "none"


def extract_duration(renewal_raw: str, eligibility_raw: str) -> str:
    if renewal_raw and renewal_raw.strip() not in ("--", "", "N/A", "None"):
        return "renewable"
    elig_lower = eligibility_raw.lower()
    if any(kw in elig_lower for kw in ["exchange", "overseas", "summer school", "trip", "attach"]):
        return "activity-based"
    return "one-time"


def extract_materials(text: str) -> list:
    checks = {
        "CV / Resume":          ["cv","curriculum vitae","resume"],
        "Personal statement":   ["personal statement","statement of purpose","essay"],
        "Transcript":           ["transcript","academic record"],
        "Reference letter":     ["reference letter","recommendation letter","referee"],
        "English test result":  ["toefl","ielts","hkdse english","english test"],
        "Portfolio":            ["portfolio"],
        "Research proposal":    ["research proposal","research plan"],
        "Interview":            ["interview"],
    }
    tl = text.lower()
    return [mat for mat, kws in checks.items() if any(k in tl for k in kws)]


def parse_scholarship_page(ss_id: int) -> Scholarship:
    url  = f"{BASE_URL}/?action=showonesscheme&ss_id={ss_id}"
    soup = get_soup(url) # Detail pages still use GET

    h1   = soup.find("h1")
    name = h1.get_text(separator=" ", strip=True) if h1 else f"Scholarship {ss_id}"
    # Strip Chinese characters if present to keep English names clean
    name = re.sub(
        u"[\u4e00-\u9fff\u3400-\u4dbf\U00020000-\U0002a6df\u300e\u300f\u300c\u300d\u3010\u3011]+",
        "", name, flags=re.UNICODE
    ).strip().rstrip(" -").strip()

    full_text = soup.get_text(separator="\n", strip=True)

    summary = {}
    for row in soup.select("table tr"):
        cells = row.find_all(["td","th"])
        if len(cells) == 2:
            summary[cells[0].get_text(strip=True).lower()] = cells[1].get_text(separator=", ", strip=True)

    value_raw       = summary.get("value of award", "")
    renewal_raw     = summary.get("renewal conditions", "--")
    type_raw        = summary.get("type of scholarships", "").lower()
    place_of_origin = summary.get("place of origin of students", "Not specified")
    level_raw       = summary.get("level of study", "")
    faculty_raw     = summary.get("faculty", "")

    types = []
    if "entrance"       in type_raw: types.append("entrance")
    if "merit"          in type_raw: types.append("merit")
    if "enrichment"     in type_raw: types.append("enrichment")
    if "financial need" in type_raw: types.append("financial_need")

    lu = re.search(r"last update[d]?\s*[:\s]+(\d{4}-\d{2}-\d{2})", full_text, re.IGNORECASE)
    last_updated = lu.group(1) if lu else ""

    eligibility_raw = full_text
    em = re.search(
        r"(?:Eligibility and Selection Criteria|Eligibility|Criteria)(.*?)(?:Value of Award|last update)",
        full_text, re.DOTALL | re.IGNORECASE
    )
    if em: eligibility_raw = em.group(1).strip()

    deadline_raw, deadline_confidence = extract_deadline(full_text)
    amount, currency = parse_amount(value_raw)

    app_url = "https://aas.hku.hk/apply-scholarships/"
    for a in soup.find_all("a", href=True):
        lt = a.get_text(strip=True).lower()
        if any(k in lt for k in ["apply","application","apply here","apply now"]) and a["href"].startswith("http"):
            app_url = a["href"]; break

    duration = extract_duration(renewal_raw, eligibility_raw)

    return Scholarship(
        id=f"ss_{ss_id}", ss_id=ss_id, name=name, source_url=url,
        provider=parse_provider(name), amount=amount, currency=currency, value_raw=value_raw,
        faculty=parse_faculty(faculty_raw), level=parse_level(level_raw),
        year_of_study=parse_year(eligibility_raw, types),
        nationality=parse_nationality(eligibility_raw, place_of_origin),
        gpa_requirement=extract_gpa(eligibility_raw),
        financial_need="financial_need" in types, merit_based="merit" in types,
        is_entrance="entrance" in types, is_enrichment="enrichment" in types,
        submission_materials=extract_materials(eligibility_raw),
        deadline_raw=deadline_raw, deadline_confidence=deadline_confidence,
        application_url=app_url,
        renewable=bool(renewal_raw and renewal_raw.strip() not in ("--","","N/A","None")),
        renewal_conditions=renewal_raw, eligibility_raw=eligibility_raw,
        place_of_origin=place_of_origin, last_updated=last_updated, duration=duration,
    )


def scrape_all(limit: Optional[int] = None, delay: float = 1.0) -> list:
    ids = get_listing_ids()
    if limit: ids = ids[:limit]
    results = []
    for i, ss_id in enumerate(ids):
        try:
            logger.info(f"Scraping {i+1}/{len(ids)}: ss_id={ss_id}")
            results.append(asdict(parse_scholarship_page(ss_id)))
            time.sleep(delay)
        except Exception as e:
            logger.error(f"Failed ss_id={ss_id}: {e}")
    logger.info(f"Scraped {len(results)}/{len(ids)}")
    return results
