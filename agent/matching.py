"""
agent/matching.py
Two-stage scholarship matching:
  Stage 1 — Azure AI Search structured filter (free, instant)
  Stage 2 — GPT-4o eligibility reasoning (one call, ~$0.05)
"""

"""
agent/matching.py
Two-stage scholarship matching:
...
"""
import os
import json
import logging
import re
from datetime import datetime, timezone, date, timedelta
from typing import Optional
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from openai import AzureOpenAI
from dotenv import load_dotenv

from agent.profile import get_profile, save_profile

load_dotenv()
logger = logging.getLogger(__name__)

# Azure AI Search
SEARCH_ENDPOINT = os.environ["AZURE_SEARCH_ENDPOINT"]
SEARCH_API_KEY  = os.environ["AZURE_SEARCH_API_KEY"]
INDEX_NAME      = os.environ.get("SCHOLARSHIP_INDEX_NAME", "scholarships")

# Azure OpenAI
openai_client = AzureOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version="2024-12-01-preview"
)
DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

# Load prompt template once
PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "eligibility_reasoning.txt")
with open(PROMPT_PATH) as f:
    PROMPT_TEMPLATE = f.read()

SCHOLARSHIP_CACHE_VERSION = 4
STAGE1_TOP_PER_QUERY = 35
STAGE1_MAX_CANDIDATES = 60
STAGE2_BATCH_SIZE = 20

FACULTY_ALIAS_GROUPS = (
    {
        "school of computing and data science", "computing and data science", "cds",
        "computer science", "computing", "data science", "faculty of engineering",
        "engineering", "faculty of engineering and",
    },
    {"faculty of business and economics", "business and economics", "business", "economics", "fba"},
    {"faculty of science", "science", "faculty of science"},
    {"faculty of arts", "arts", "humanities"},
    {"faculty of medicine", "medicine", "li ka shing", "medical"},
    {"faculty of law", "law"},
)

_EXCLUSIVE_PROGRAMME_MARKERS = (
    "only for", " exclusively", "must be enrolled in", "restricted to",
    "open to students of the", "students of the programme",
)

STAGE1_SELECT = [
    "id", "name", "faculty", "level", "year_of_study", "nationality",
    "gpa_requirement", "financial_need", "merit_based", "is_entrance",
    "is_enrichment", "deadline_raw", "deadline_iso", "is_open",
    "application_method", "application_url", "submission_materials",
    "eligibility_raw", "amount", "currency", "provider", "duration",
    "place_of_origin", "renewal_conditions",
]

ENTRANCE_TEXT_TERMS = (
    "entrance scholarship",
    "entrance award",
    "entrance fellow",
    "for new students admitted",
    "upon admission to hku",
    "upon admission to the university",
    "at the time of admission",
    "jupas applicant",
    "non-jupas applicant",
    "first-year admission",
    "admitted to hku",
)


def _is_entrance_scholarship(scholarship: dict) -> bool:
    """Entrance awards are for prospective admits, not current HKU students."""
    if scholarship.get("is_entrance") is True:
        return True

    years = scholarship.get("year_of_study")
    if years in (None, "", []):
        normalized_years: set[str] = set()
    elif isinstance(years, list):
        normalized_years = {str(value).strip().lower() for value in years if str(value).strip()}
    else:
        normalized_years = {str(years).strip().lower()}

    if normalized_years == {"new_student"}:
        return True

    text = " ".join(
        str(scholarship.get(field) or "")
        for field in ("name", "eligibility_raw", "application_method")
    ).lower()
    if "entrance" in text and any(term in text for term in ("scholarship", "award", "fellowship", "bursary")):
        return True
    return any(term in text for term in ENTRANCE_TEXT_TERMS)


# ---------------------------------------------------------------------------
# Stage 1 — Structured Azure AI Search filter
# ---------------------------------------------------------------------------

def _student_gpa(profile: dict) -> float:
    try:
        return float(profile.get("academic", {}).get("gpa") or 0)
    except (TypeError, ValueError):
        return 0.0


def _faculty_alias_tokens(faculty: str) -> set[str]:
    text = str(faculty or "").strip().lower()
    tokens = {text} if text else set()
    for group in FACULTY_ALIAS_GROUPS:
        if any(alias in text or text in alias for alias in group):
            tokens |= group
    return tokens


def _faculty_matches(index_faculties, student_faculty: str) -> bool:
    """Loose faculty match — handles CDS vs Engineering and university-wide awards."""
    if not student_faculty:
        return True
    if index_faculties in (None, "", []):
        return True
    if not isinstance(index_faculties, list):
        index_faculties = [index_faculties]
    student = str(student_faculty).strip().lower()
    student_tokens = _faculty_alias_tokens(student)
    for value in index_faculties:
        faculty = str(value).strip().lower()
        if not faculty or faculty == "all":
            return True
        if student == faculty or student in faculty or faculty in student:
            return True
        if student_tokens & _faculty_alias_tokens(faculty):
            return True
    return False


def _normalize_year_token(value) -> str:
    token = str(value or "").strip().lower()
    if not token:
        return ""
    if token in {"year 1", "year1", "y1", "1st year", "first year"}:
        return "1"
    if token in {"year 2", "year2", "y2", "2nd year", "second year"}:
        return "2"
    if token in {"year 3", "year3", "y3", "3rd year", "third year"}:
        return "3"
    if token in {"year 4", "year4", "y4", "4th year", "fourth year", "final year", "final"}:
        return "4"
    match = re.search(r"\b([1-4])\b", token)
    if match and "year" in token:
        return match.group(1)
    if token.isdigit() and token in {"1", "2", "3", "4"}:
        return token
    return token


def _year_matches(index_years, student_year: str) -> bool:
    if index_years in (None, "", []):
        return True
    if not isinstance(index_years, list):
        index_years = [index_years]
    student = _normalize_year_token(student_year)
    normalized = {_normalize_year_token(value) for value in index_years if str(value).strip()}
    normalized = {tag for tag in normalized if tag}
    if not normalized or normalized == {"all"}:
        return True
    if student in normalized:
        return True
    if student == "3" and "penultimate" in normalized:
        return True
    if student == "4" and "final" in normalized:
        return True
    return False


def _programme_keyword_groups(text: str) -> set[str]:
    text = text.lower()
    keyword_groups = {
        "surveying": ("surveying", "surveyor", "real estate", "urban planning"),
        "architecture": ("architecture", "architectural studies", "architectural"),
        "civil engineering": ("civil engineering", "civil engineer"),
        "computer science": (
            "computer science", "computing", "data science", "artificial intelligence",
            "ai", "information systems", "software engineering",
        ),
        "medicine": ("medicine", "medical", "mbbs", "clinical"),
        "law": ("law", "legal", "llb"),
        "business": ("business", "economics", "finance", "accounting", "bba"),
        "engineering": ("engineering", "engineer", "innovation", "technology"),
        "education": ("education", "teaching"),
        "social sciences": ("social sciences", "social science", "psychology", "sociology"),
        "arts": ("arts", "humanities", "literature", "linguistics", "history"),
        "science": ("science", "physics", "chemistry", "biology", "mathematics"),
        "dentistry": ("dentistry", "dental"),
    }
    found = set()
    for label, keywords in keyword_groups.items():
        if any(keyword in text for keyword in keywords):
            found.add(label)
    return found


def _student_programme_groups(programme: str, faculty: str) -> set[str]:
    groups = _programme_keyword_groups(programme)
    faculty_lower = str(faculty or "").lower()
    if any(term in faculty_lower for term in ("computing", "data science", "cds", "computer")):
        groups.update({"computer science", "engineering"})
    if "engineering" in faculty_lower:
        groups.add("engineering")
    return groups


def _combined_requirement_text(item: dict) -> str:
    parts = [
        item.get("name", ""),
        item.get("eligibility_raw", ""),
        item.get("application_method", ""),
        item.get("reason", ""),
        item.get("gap", ""),
    ]
    materials = item.get("submission_materials")
    if isinstance(materials, list):
        parts.extend(str(part) for part in materials)
    else:
        parts.append(str(materials or ""))
    return " ".join(str(part or "") for part in parts).lower()


def _deterministic_program_match(item: dict, profile: dict) -> str:
    academic = profile.get("academic", {})
    student_programme = str(academic.get("programme", "")).lower()
    student_faculty = str(academic.get("faculty", "")).lower()
    result_program_match = str(item.get("program_match") or "").strip().lower()
    if result_program_match in ("exact", "faculty_only", "mismatch"):
        return result_program_match

    text = _combined_requirement_text(item)
    scholarship_programmes = _programme_keyword_groups(text)
    student_programmes = _student_programme_groups(student_programme, student_faculty)

    if scholarship_programmes and student_programmes.intersection(scholarship_programmes):
        return "exact"
    if not scholarship_programmes:
        return "faculty_only"
    if any(marker in text for marker in _EXCLUSIVE_PROGRAMME_MARKERS):
        if student_programmes.intersection(scholarship_programmes):
            return "exact"
        return "mismatch"
    if _faculty_matches(item.get("faculty"), student_faculty):
        return "faculty_only"
    faculties = item.get("faculty") or []
    if not faculties or "all" in {str(value).strip().lower() for value in faculties}:
        return "faculty_only"
    return "mismatch"


def _core_requirements_met(item: dict, profile: dict) -> bool:
    academic = profile.get("academic", {})
    financial = profile.get("financial", {})

    def value_matches_student(values, student_value: str) -> bool:
        if values in (None, "", []):
            return True
        if not isinstance(values, list):
            values = [values]
        normalized = {str(value).strip().lower() for value in values if str(value).strip()}
        return not normalized or "all" in normalized or str(student_value).strip().lower() in normalized

    local_status = academic.get("nationality", {}).get("local_status", "local")
    if not _faculty_matches(item.get("faculty"), academic.get("faculty", "")):
        return False
    if not value_matches_student(item.get("level"), academic.get("level", "undergraduate")):
        return False
    if not _year_matches(item.get("year_of_study"), str(academic.get("year_of_study", ""))):
        return False
    if not value_matches_student(item.get("nationality"), local_status):
        return False
    if item.get("financial_need") and not financial.get("financial_need_opt_in", False):
        return False

    gpa_requirement = item.get("gpa_requirement")
    student_gpa = academic.get("gpa", 0.0)
    if gpa_requirement not in (None, ""):
        try:
            if float(student_gpa or 0) < float(gpa_requirement):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _stage1_search_queries(profile: dict) -> list[str]:
    academic = profile.get("academic", {})
    faculty = str(academic.get("faculty") or "").strip()
    programme = str(academic.get("programme") or "").strip()
    queries = ["scholarship", "innovation", "merit", "enrichment"]
    if faculty:
        queries.insert(0, faculty)
        for alias in sorted(_faculty_alias_tokens(faculty)):
            if len(alias) > 3:
                queries.append(alias)
    if programme:
        queries.insert(0, programme)
    for interest in (profile.get("interests") or [])[:4]:
        token = str(interest).strip()
        if token:
            queries.append(token)
    seen = set()
    unique = []
    for query in queries:
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(query)
    return unique


def _build_stage1_filter(profile: dict) -> str | None:
    academic = profile.get("academic", {})
    level = academic.get("level", "undergraduate")
    gpa = academic.get("gpa", 0.0)
    local_status = academic.get("nationality", {}).get("local_status", "local")

    filters = [
        f"(level/any(l: l eq '{level}') or level/any(l: l eq 'all'))",
        "not (is_entrance eq true)",
    ]
    if gpa and gpa > 0:
        filters.append(f"(gpa_requirement eq null or gpa_requirement le {gpa})")
    if local_status == "local":
        filters.append("(nationality/any(n: n eq 'local') or nationality/any(n: n eq 'all'))")
    else:
        filters.append("(nationality/any(n: n eq 'non-local') or nationality/any(n: n eq 'all'))")
    return " and ".join(filters)


def _accept_stage1_candidate(candidate: dict, profile: dict) -> bool:
    academic = profile.get("academic", {})
    if _is_entrance_scholarship(candidate):
        return False
    if not _faculty_matches(candidate.get("faculty"), academic.get("faculty", "")):
        return False
    if not _year_matches(candidate.get("year_of_study"), str(academic.get("year_of_study", 1))):
        return False
    return True


PROTOTYPE_SCHOLARSHIP_472 = {
    "id": "ss_472",
    "scholarship_id": "ss_472",
    "name": "D. H. Chen Foundation Scholarship",
    "qualifies": True,
    "match_strength": "strong",
    "program_match": "faculty_only",
    "reason": "Your GPA meets the 3.5 minimum requirement for this scholarship.",
    "gap": None,
    "application_notes": "Tap Start Application to upload and auto-fill the form.",
    "deadline_raw": "See scholarship page for deadline",
    "deadline_iso": None,
    "application_method": "https://aas.hku.hk/apply-scholarships/",
    "application_url": "https://scholar.aas.hku.hk/?action=showonesscheme&ss_id=472",
    "source_url": "https://scholar.aas.hku.hk/?action=showonesscheme&ss_id=472",
    "is_prototype": True,
    "gpa_requirement": 3.5,
}


def _inject_prototype_scholarship_472(result: dict, profile: dict) -> dict:
    """Always include ss_472 in results when the student GPA is at least 3.5."""
    if _student_gpa(profile) < 3.5:
        return result

    existing_ids = {
        str(item.get("scholarship_id") or item.get("id") or "")
        for item in result.get("apply_now", []) + result.get("prepare", [])
    }
    if "ss_472" in existing_ids:
        return result

    prepare = list(result.get("prepare", []))
    prepare.insert(0, PROTOTYPE_SCHOLARSHIP_472.copy())
    result["prepare"] = prepare
    result["prepare_count"] = len(prepare)
    return result


def _scholarship_identifier(item: dict) -> str:
    return str(item.get("scholarship_id") or item.get("id") or item.get("name") or "unknown")


def _source_requirement_text(item: dict) -> str:
    parts = [
        item.get("eligibility_raw", ""),
        item.get("application_method", ""),
        item.get("name", ""),
    ]
    materials = item.get("submission_materials")
    if isinstance(materials, list):
        parts.extend(str(part) for part in materials)
    else:
        parts.append(str(materials or ""))
    return " ".join(str(part or "") for part in parts).lower()


def _remove_unsupported_application_notes(item: dict) -> None:
    note = str(item.get("application_notes") or "").strip()
    if not note:
        return
    note_text = note.lower()
    source_text = _source_requirement_text(item)
    requirement_terms = ("reference", "referee", "interview", "essay", "personal statement", "portfolio")
    if any(term in note_text for term in requirement_terms) and not any(term in source_text for term in requirement_terms):
        logger.info(
            "Removed unsupported application note for %s: %s",
            _scholarship_identifier(item),
            note,
        )
        item["application_notes"] = None


def _has_non_closeable_gap(item: dict) -> bool:
    gap_text = str(item.get("gap") or "").strip().lower()
    if not gap_text or gap_text in ("none", "null", "n/a"):
        return False
    non_closeable_terms = (
        "resident", "residence", "hall", "r.c. lee", "rc lee",
        "financial need", "opted out", "wrong faculty", "wrong programme",
        "wrong program", "wrong year", "wrong level", "wrong nationality",
        "non-local", "local status", "gpa below", "below requirement",
        "postgraduate only", "undergraduate only", "publication",
        "published paper", "patent", "certification", "national award",
        "international award", "not eligible", "does not meet",
        "exchange student", "exchange status", "specific programme",
        "specific program", "not enrolled", "not a resident", "not from",
        "college", "school requirement",
    )
    return any(term in gap_text for term in non_closeable_terms)


def _strict_result_filter(results: list[dict], profile: dict) -> list[dict]:
    strict_results = []
    for result in results:
        scholarship_id = _scholarship_identifier(result)
        if _is_entrance_scholarship(result):
            logger.info(
                "Filtered out %s: entrance scholarship (out of scope for enrolled students)",
                scholarship_id,
            )
            continue
        if result.get("qualifies") is not True:
            continue
        if str(result.get("match_strength", "")).lower() != "strong":
            continue
        if _has_non_closeable_gap(result):
            logger.info(
                "Filtered out %s: hard requirement mismatch - %s",
                scholarship_id,
                result.get("gap"),
            )
            continue
        if not _core_requirements_met(result, profile):
            logger.info(
                "Filtered out %s: hard requirement mismatch - core requirements not met",
                scholarship_id,
            )
            continue
        program_match = _deterministic_program_match(result, profile)
        result["program_match"] = program_match
        if program_match not in ("exact", "faculty_only"):
            logger.info(
                "Filtered out %s: hard requirement mismatch - program_match=%s",
                scholarship_id,
                program_match,
            )
            continue
        _remove_unsupported_application_notes(result)
        strict_results.append(result)
    return strict_results


def _fallback_scholarship_matches(profile: dict, candidates: list[dict]) -> list[dict]:
    """Heuristic fallback when LLM returns too few matches."""
    results = []
    for candidate in candidates:
        if not _core_requirements_met(candidate, profile):
            continue
        program_match = _deterministic_program_match(candidate, profile)
        if program_match == "mismatch":
            continue
        merged = {
            **candidate,
            "scholarship_id": candidate.get("id"),
            "qualifies": True,
            "match_strength": "strong",
            "program_match": program_match,
            "reason": (
                f"You meet the core eligibility criteria for {candidate.get('name', 'this scholarship')}."
            ),
            "gap": None,
        }
        results.append(merged)
    return results


def _candidate_payload(candidate: dict) -> dict:
    return {
        "id":               candidate.get("id"),
        "name":             candidate.get("name"),
        "faculty":          candidate.get("faculty"),
        "level":            candidate.get("level"),
        "year_of_study":    candidate.get("year_of_study"),
        "nationality":      candidate.get("nationality"),
        "gpa_requirement":  candidate.get("gpa_requirement"),
        "financial_need":   candidate.get("financial_need"),
        "merit_based":      candidate.get("merit_based"),
        "is_entrance":      candidate.get("is_entrance"),
        "is_enrichment":    candidate.get("is_enrichment"),
        "amount":           f"{candidate.get('amount', '')} {candidate.get('currency', 'HKD')}",
        "provider":         candidate.get("provider"),
        "is_open":          candidate.get("is_open", False),
        "deadline_raw":     candidate.get("deadline_raw", ""),
        "deadline_iso":     candidate.get("deadline_iso"),
        "application_method": candidate.get("application_method", ""),
        "application_url":  candidate.get("application_url"),
        "source_url":       candidate.get("source_url"),
        "submission_materials": candidate.get("submission_materials"),
        "eligibility_raw":  candidate.get("eligibility_raw", "")[:400],
    }


def _parse_stage2_raw(raw: str) -> list[dict]:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    parsed = json.loads(text)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return next((v for v in parsed.values() if isinstance(v, list)), [])
    return []


def _enrich_stage2_results(results: list[dict], batch: list[dict]) -> list[dict]:
    candidate_lookup = {str(c.get("id")): c for c in batch if c.get("id")}
    enriched = []
    for result in results:
        scholarship_id = str(result.get("scholarship_id") or result.get("id") or "")
        candidate = candidate_lookup.get(scholarship_id, {})
        enriched.append({
            **candidate,
            **result,
            "scholarship_id": scholarship_id or candidate.get("id"),
            "application_url": result.get("application_url") or candidate.get("application_url"),
            "source_url": result.get("source_url") or candidate.get("source_url"),
            "submission_materials": result.get("submission_materials") or candidate.get("submission_materials"),
        })
    return enriched


def _stage1_search(profile: dict) -> list[dict]:
    """
    Filter scholarships using structured fields plus multiple search queries.
    Returns a broad candidate set for Stage 2 LLM reasoning.
    """
    filter_str = _build_stage1_filter(profile)
    client = SearchClient(SEARCH_ENDPOINT, INDEX_NAME, AzureKeyCredential(SEARCH_API_KEY))

    seen_ids: set[str] = set()
    candidates: list[dict] = []
    queries = _stage1_search_queries(profile)

    def add_results(results) -> None:
        for candidate in results:
            candidate_id = str(candidate.get("id") or "")
            if not candidate_id or candidate_id in seen_ids:
                continue
            if not _accept_stage1_candidate(candidate, profile):
                continue
            seen_ids.add(candidate_id)
            candidates.append(candidate)

    for search_text in queries:
        if len(candidates) >= STAGE1_MAX_CANDIDATES:
            break
        search_kwargs = {
            "search_text": search_text,
            "select": STAGE1_SELECT,
            "top": STAGE1_TOP_PER_QUERY,
        }
        if filter_str:
            search_kwargs["filter"] = filter_str
        add_results(client.search(**search_kwargs))

    logger.info(
        "Stage 1: %s candidates after structured filter (%s search queries)",
        len(candidates),
        len(queries),
    )
    return candidates[:STAGE1_MAX_CANDIDATES]


# ---------------------------------------------------------------------------
# Stage 2 — GPT-4o eligibility reasoning
# ---------------------------------------------------------------------------

def _stage2_reasoning(profile: dict, candidates: list[dict]) -> list[dict]:
    """LLM eligibility reasoning over Stage 1 candidates, with heuristic fallback."""
    if not candidates:
        return []

    academic = profile.get("academic", {})
    financial = profile.get("financial", {})
    profile_summary = {
        "name": profile.get("name", "Student"),
        "faculty": academic.get("faculty", ""),
        "programme": academic.get("programme", ""),
        "year_of_study": academic.get("year_of_study", 1),
        "gpa": academic.get("gpa", 0.0),
        "level": academic.get("level", "undergraduate"),
        "local_status": academic.get("nationality", {}).get("local_status", "local"),
        "country_of_origin": academic.get("nationality", {}).get("country_of_origin", "Hong Kong"),
        "financial_need_opt_in": financial.get("financial_need_opt_in", False),
        "interests": profile.get("interests", []),
        "activities": profile.get("activities", []),
        "cv_summary": profile.get("cv_text", "")[:300],
        "upcoming_deadlines": profile.get("timetable", {}).get("upcoming_deadlines", []),
    }

    def run_batch(batch: list[dict], allow_retry: bool = True) -> list[dict]:
        scholarship_list = [_candidate_payload(c) for c in batch]
        prompt = PROMPT_TEMPLATE.format(
            student_profile=json.dumps(profile_summary, indent=2, ensure_ascii=False),
            scholarship_candidates=json.dumps(scholarship_list, indent=2, ensure_ascii=False),
        )
        raw = ""
        finish_reason = None
        try:
            response = openai_client.chat.completions.create(
                model=DEPLOYMENT,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=6000,
                temperature=0.1,
            )
            choice = response.choices[0]
            raw = choice.message.content or ""
            finish_reason = getattr(choice, "finish_reason", None)
            parsed = _parse_stage2_raw(raw)
            return _enrich_stage2_results(parsed, batch)
        except json.JSONDecodeError as exc:
            logger.error("Stage 2 JSON parse error: %s; finish_reason=%s", exc, finish_reason)
            logger.error("Raw Stage 2 response was: %r", raw)
            if allow_retry and len(batch) > 1:
                midpoint = max(1, len(batch) // 2)
                logger.info(
                    "Retrying Stage 2 in smaller batches: %s and %s",
                    midpoint,
                    len(batch) - midpoint,
                )
                return run_batch(batch[:midpoint], allow_retry=False) + run_batch(batch[midpoint:], allow_retry=False)
            return []
        except Exception as exc:
            logger.error("Stage 2 OpenAI error: %s", exc)
            return []

    llm_results: list[dict] = []
    for start in range(0, len(candidates), STAGE2_BATCH_SIZE):
        batch = candidates[start:start + STAGE2_BATCH_SIZE]
        llm_results.extend(run_batch(batch))

    results = _strict_result_filter(llm_results, profile)
    if len(results) < 3:
        seen = {_scholarship_identifier(item) for item in results}
        for fallback in _fallback_scholarship_matches(profile, candidates):
            scholarship_id = _scholarship_identifier(fallback)
            if scholarship_id in seen:
                continue
            results.append(fallback)
            seen.add(scholarship_id)

    logger.info("Stage 2: %s strong qualified matches returned", len(results))
    return results


# ---------------------------------------------------------------------------
# Stage 3 — Sort and package
# ---------------------------------------------------------------------------

def _sort_and_package(matches: list) -> dict:
    apply_now = []
    prepare   = []

    today = date.today()

    def days_until_deadline(item: dict) -> int | None:
        deadline = item.get("deadline_iso")
        if not deadline:
            return None
        try:
            return (date.fromisoformat(str(deadline)[:10]) - today).days
        except ValueError:
            return None

    def requires_preparation(item: dict) -> bool:
        materials = item.get("submission_materials") or item.get("application_notes") or ""
        if isinstance(materials, list):
            materials_text = " ".join(str(part) for part in materials)
        else:
            materials_text = str(materials)
        materials_text = materials_text.lower()
        return any(keyword in materials_text for keyword in ("cv", "reference", "referee", "transcript", "proposal"))

    # Apply Now is strictly open and due within 30 days; later/prep-heavy items go into Prepare.
    for m in matches:
        days_left = days_until_deadline(m)
        if (
            m.get("is_open")
            and days_left is not None
            and 0 <= days_left <= 30
            and not requires_preparation(m)
        ):
            apply_now.append(m)
        else:
            m["tier"] = "prepare"
            prepare.append(m)

    # Sort apply_now: deadline ascending then strength
    strength_order = {"strong": 0}
    program_order = {"exact": 0, "faculty_only": 1}
    apply_now.sort(key=lambda m: (
        program_order.get(m.get("program_match", "faculty_only"), 1),
        m.get("deadline_iso") or "9999-12-31",
        strength_order.get(m.get("match_strength", "strong"), 1)
    ))

    # Sort prepare: earlier deadlines first, then strength.
    tier_order = {"prepare": 0}
    prepare.sort(key=lambda m: (
        program_order.get(m.get("program_match", "faculty_only"), 1),
        tier_order.get(m.get("tier", "prepare"), 0),
        m.get("deadline_iso") or "9999-12-31",
        strength_order.get(m.get("match_strength", "strong"), 1)
    ))

    return {
        "apply_now":       apply_now,
        "prepare":         prepare,
        "apply_now_count": len(apply_now),
        "prepare_count":   len(prepare),
        "run_at":          datetime.now(timezone.utc).isoformat(),
    }

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_matching(student_id: str) -> dict:
    """
    Full matching pipeline for a student.
    Returns structured digest-ready output.
    """
    profile = get_profile(student_id)
    if not profile:
        logger.error(f"Profile not found: {student_id}")
        return {"error": f"Profile not found: {student_id}"}

    cache = profile.get("scholarship_cache") or {}
    cached_result = cache.get("result")
    cached_timestamp = cache.get("timestamp")
    cached_version = cache.get("version", 1)
    if cached_result and cached_timestamp and cached_version >= SCHOLARSHIP_CACHE_VERSION:
        try:
            cached_at = datetime.fromisoformat(str(cached_timestamp).replace("Z", "+00:00"))
            if cached_at.tzinfo is None:
                cached_at = cached_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - cached_at < timedelta(hours=1):
                logger.info(f"Using cached scholarship matches for {student_id}")
                return cached_result
        except (TypeError, ValueError):
            logger.warning(f"Ignoring invalid scholarship cache timestamp for {student_id}")

    logger.info(f"Running matching for {profile.get('name', student_id)}...")

    candidates = _stage1_search(profile)
    if not candidates:
        logger.warning("No candidates from Stage 1 — check index has data")
        result = {
            "apply_now": [],
            "prepare": [],
            "apply_now_count": 0,
            "prepare_count": 0,
            "student_id": student_id,
            "student_name": profile.get("name", "")
        }
        result = _inject_prototype_scholarship_472(result, profile)
        profile["scholarship_cache"] = {
            "result": result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version": SCHOLARSHIP_CACHE_VERSION,
        }
        save_profile(profile)
        return result

    matches     = _stage2_reasoning(profile, candidates)
    matched     = [
        m for m in matches
        if m.get("qualifies") is True and str(m.get("match_strength", "")).lower() == "strong"
    ]
    result      = _sort_and_package(matched)
    result      = _inject_prototype_scholarship_472(result, profile)
    result["student_id"]   = student_id
    result["student_name"] = profile.get("name", "")
    profile["scholarship_cache"] = {
        "result": result,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": SCHOLARSHIP_CACHE_VERSION,
    }
    save_profile(profile)
    return result


def find_scholarship_by_query(student_id: str, query: str, scholarship_result: dict | None = None) -> dict | None:
    """
    Resolve a scholarship name fragment to a match from cached or fresh results.
    Returns the best matching scholarship dict, or None.
    """
    query = (query or "").strip().lower()
    if not query:
        return None

    if scholarship_result is None:
        profile = get_profile(student_id)
        cache = (profile or {}).get("scholarship_cache") or {}
        scholarship_result = cache.get("result") or {}
        if not scholarship_result.get("apply_now") and not scholarship_result.get("prepare"):
            try:
                scholarship_result = run_matching(student_id)
            except Exception as exc:
                logger.warning("find_scholarship_by_query matching failed: %s", exc)
                scholarship_result = {}

    candidates: list[dict] = []
    for tier in ("apply_now", "prepare"):
        candidates.extend(scholarship_result.get(tier) or [])

    chen_aliases = ("chen", "d. h. chen", "dh chen", "d h chen")
    if any(alias in query for alias in chen_aliases):
        for candidate in candidates:
            cid = str(candidate.get("scholarship_id") or candidate.get("id") or "")
            if cid == "ss_472" or candidate.get("is_prototype"):
                return candidate
        if _student_gpa(get_profile(student_id) or {}) >= 3.5:
            return PROTOTYPE_SCHOLARSHIP_472.copy()

    best: dict | None = None
    best_score = 0
    for candidate in candidates:
        name = str(candidate.get("name") or "").lower()
        if not name:
            continue
        if query in name:
            score = 100 + len(query)
        elif name in query:
            score = 80
        else:
            tokens = [token for token in query.split() if len(token) >= 3]
            score = sum(10 for token in tokens if token in name)
        if score > best_score:
            best_score = score
            best = candidate

    return best if best_score >= 10 else None


# ---------------------------------------------------------------------------
# Local test — run from hku_agent/ folder
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    mock_profile = {
        "id": "local_mock_student",
        "name": "Local Mock Student",
        "email": "local.mock.student@connect.hku.hk",
        "academic": {
            "faculty": "Engineering",
            "programme": "Bachelor of Engineering in Computer Science",
            "year_of_study": 2,
            "gpa": 3.7,
            "level": "undergraduate",
            "nationality": {
                "local_status": "local",
                "country_of_origin": "Hong Kong"
            },
            "expected_graduation_year": 2028,
        },
        "financial": {"financial_need_opt_in": False},
        "interests": ["AI", "robotics", "hackathons"],
        "activities": ["HKU Robotics Team", "Undergraduate research assistant"],
        "cv_text": "Engineering student with robotics, research, and hackathon experience.",
        "timetable": {"upcoming_deadlines": []},
    }
    get_profile = lambda student_id: mock_profile

    print("\n" + "="*60)
    print("MATCHING TEST — Local Mock Student")
    print("="*60)

    result = run_matching(mock_profile["id"])

    if "error" in result:
        print(f"ERROR: {result['error']}")
    else:
        print(f"\nApply now: {result['apply_now_count']}  |  Prepare: {result['prepare_count']}")

        print("\n" + "="*60)
        print("TIER 1 — APPLY NOW")
        print("="*60)
        for m in result["apply_now"]:
            print(f"\n  {m['name']}")
            print(f"  Strength : {m['match_strength']}")
            print(f"  Deadline : {m.get('deadline_raw','N/A')}")
            print(f"  Reason   : {m['reason']}")
            if m.get("application_notes"):
                print(f"  Notes    : {m['application_notes']}")
            if m.get("calendar_note"):
                print(f"  Calendar : {m['calendar_note']}")

        print("\n" + "="*60)
        print("TIER 2 — PREPARE")
        print("="*60)
        for m in result["prepare"]:
            print(f"\n  [STRONG] {m['name']}")
            print(f"  Strength : {m.get('match_strength','strong')}")
            print(f"  Deadline : {m.get('deadline_raw','N/A')}")
            if m.get("reason"):
                print(f"  Reason   : {m['reason']}")
            if m.get("gap"):
                print(f"  Gap      : {m['gap']}")
            if m.get("application_notes"):
                print(f"  Strengthen: {m['application_notes']}")

