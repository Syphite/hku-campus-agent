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
from datetime import datetime, timezone, date
from typing import Optional
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from openai import AzureOpenAI
from dotenv import load_dotenv

from agent.profile import get_profile

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


# ---------------------------------------------------------------------------
# Stage 1 — Structured Azure AI Search filter
# ---------------------------------------------------------------------------

def _stage1_search(profile: dict) -> list[dict]:
    """
    Filter scholarships using structured fields.
    Returns 15-25 candidates for Stage 2 LLM reasoning.
    Costs essentially nothing — just a database query.
    """
    academic  = profile.get("academic", {})
    financial = profile.get("financial", {})

    faculty      = academic.get("faculty", "")
    level        = academic.get("level", "undergraduate")
    gpa          = academic.get("gpa", 0.0)
    local_status = academic.get("nationality", {}).get("local_status", "local")
    need_opt_in  = financial.get("financial_need_opt_in", False)
    year         = str(academic.get("year_of_study", 1))

    # Build OData filter
    filters = []

    # Faculty: match student faculty OR "all"
    filters.append(f"(faculty/any(f: f eq '{faculty}') or faculty/any(f: f eq 'all'))")

    # Level: match student level OR "all"
    filters.append(f"(level/any(l: l eq '{level}') or level/any(l: l eq 'all'))")

    # GPA: no requirement, or requirement student meets. No near-miss buffer.
    if gpa and gpa > 0:
        filters.append(f"(gpa_requirement eq null or gpa_requirement le {gpa})")

    # Year: match student year OR all when the index has structured year data.
    filters.append(f"(year_of_study/any(y: y eq '{year}') or year_of_study/any(y: y eq 'all'))")

    # Financial need: if student opted out, exclude need-only scholarships
    if not need_opt_in:
        # Include scholarships that are not purely need-based
        # (merit=True covers most; enrichment and entrance also fine)
        filters.append("(merit_based eq true or is_enrichment eq true or is_entrance eq true or financial_need eq false)")

    # Nationality: local/non-local
    if local_status == "local":
        filters.append("(nationality/any(n: n eq 'local') or nationality/any(n: n eq 'all'))")
    else:
        filters.append("(nationality/any(n: n eq 'non-local') or nationality/any(n: n eq 'all'))")

    filter_str = " and ".join(filters)

    client  = SearchClient(SEARCH_ENDPOINT, INDEX_NAME, AzureKeyCredential(SEARCH_API_KEY))
    results = client.search(
        search_text="*",
        filter=filter_str,
        select=[
            "id", "name", "faculty", "level", "year_of_study", "nationality",
            "gpa_requirement", "financial_need", "merit_based", "is_entrance",
            "is_enrichment", "deadline_raw", "deadline_iso", "is_open",
            "application_method", "application_url", "submission_materials",
            "eligibility_raw", "amount", "currency", "provider", "duration",
            "place_of_origin", "renewal_conditions"
        ],
        top=15
    )

    candidates = list(results)
    logger.info(f"Stage 1: {len(candidates)} candidates after structured filter")
    return candidates


# ---------------------------------------------------------------------------
# Stage 2 — GPT-4o eligibility reasoning
# ---------------------------------------------------------------------------

def _stage2_reasoning(profile: dict, candidates: list[dict]) -> list[dict]:
    """
    One GPT-4o call to reason over all candidates.
    Returns qualified and near-miss scholarships with reasons.
    """
    if not candidates:
        return []

    # Build a compact profile summary for the prompt
    academic  = profile.get("academic", {})
    financial = profile.get("financial", {})

    profile_summary = {
        "name":             profile.get("name", "Student"),
        "faculty":          academic.get("faculty", ""),
        "programme":        academic.get("programme", ""),
        "year_of_study":    academic.get("year_of_study", 1),
        "gpa":              academic.get("gpa", 0.0),
        "level":            academic.get("level", "undergraduate"),
        "local_status":     academic.get("nationality", {}).get("local_status", "local"),
        "country_of_origin":academic.get("nationality", {}).get("country_of_origin", "Hong Kong"),
        "financial_need_opt_in": financial.get("financial_need_opt_in", False),
        "interests":        profile.get("interests", []),
        "activities":       profile.get("activities", []),
        "cv_summary":       profile.get("cv_text", "")[:300],  # first 1000 chars
        "upcoming_deadlines": profile.get("timetable", {}).get("upcoming_deadlines", []),
    }

    def value_matches_student(values, student_value: str) -> bool:
        if values in (None, "", []):
            return True
        if not isinstance(values, list):
            values = [values]
        normalized = {str(value).strip().lower() for value in values if str(value).strip()}
        return not normalized or "all" in normalized or str(student_value).strip().lower() in normalized

    def core_requirements_met(item: dict) -> bool:
        academic = profile.get("academic", {})
        financial = profile.get("financial", {})
        local_status = academic.get("nationality", {}).get("local_status", "local")

        if not value_matches_student(item.get("faculty"), academic.get("faculty", "")):
            return False
        if not value_matches_student(item.get("level"), academic.get("level", "undergraduate")):
            return False
        if not value_matches_student(item.get("year_of_study"), str(academic.get("year_of_study", ""))):
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

    def has_non_closeable_gap(item: dict) -> bool:
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
            "international award", "not eligible", "does not meet"
        )
        return True if gap_text else any(term in gap_text for term in non_closeable_terms)

    def strict_result_filter(results: list[dict]) -> list[dict]:
        strict_results = []
        for result in results:
            if result.get("qualifies") is not True:
                continue
            if str(result.get("match_strength", "")).lower() != "strong":
                continue
            if has_non_closeable_gap(result):
                continue
            if not core_requirements_met(result):
                continue
            strict_results.append(result)
        return strict_results

    def candidate_payload(candidate: dict) -> dict:
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

    def parse_stage2_raw(raw: str) -> list[dict]:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return next((v for v in parsed.values() if isinstance(v, list)), [])
        return []

    def enrich_results(results: list[dict], batch: list[dict]) -> list[dict]:
        candidate_lookup = {str(c.get("id")): c for c in batch if c.get("id")}
        enriched = []
        for result in results:
            scholarship_id = str(result.get("scholarship_id") or result.get("id") or "")
            candidate = candidate_lookup.get(scholarship_id, {})
            merged = {
                **candidate,
                **result,
                "scholarship_id": scholarship_id or candidate.get("id"),
                "application_url": result.get("application_url") or candidate.get("application_url"),
                "source_url": result.get("source_url") or candidate.get("source_url"),
                "submission_materials": result.get("submission_materials") or candidate.get("submission_materials"),
            }
            enriched.append(merged)
        return enriched

    def run_batch(batch: list[dict], allow_retry: bool = True) -> list[dict]:
        scholarship_list = [candidate_payload(c) for c in batch]
        prompt = PROMPT_TEMPLATE.format(
            student_profile=json.dumps(profile_summary, indent=2, ensure_ascii=False),
            scholarship_candidates=json.dumps(scholarship_list, indent=2, ensure_ascii=False)
        )

        raw = ""
        finish_reason = None
        try:
            response = openai_client.chat.completions.create(
                model=DEPLOYMENT,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=6000,
                temperature=0.1
            )
            choice = response.choices[0]
            raw = choice.message.content or ""
            finish_reason = getattr(choice, "finish_reason", None)
            results = parse_stage2_raw(raw)
            return enrich_results(results, batch)
        except json.JSONDecodeError as e:
            logger.error(f"Stage 2 JSON parse error: {e}; finish_reason={finish_reason}")
            logger.error(f"Raw Stage 2 response was: {raw!r}")
            if allow_retry and len(batch) > 1:
                midpoint = max(1, len(batch) // 2)
                logger.info(f"Retrying Stage 2 in smaller batches: {midpoint} and {len(batch) - midpoint}")
                return run_batch(batch[:midpoint], allow_retry=False) + run_batch(batch[midpoint:], allow_retry=False)
            return []
        except Exception as e:
            logger.error(f"Stage 2 OpenAI error: {e}")
            return []

    results = strict_result_filter(run_batch(candidates))
    logger.info(f"Stage 2: {len(results)} strong qualified matches returned")
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
    apply_now.sort(key=lambda m: (
        m.get("deadline_iso") or "9999-12-31",
        strength_order.get(m.get("match_strength", "strong"), 1)
    ))

    # Sort prepare: earlier deadlines first, then strength.
    tier_order = {"prepare": 0}
    prepare.sort(key=lambda m: (
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

    logger.info(f"Running matching for {profile.get('name', student_id)}...")

    candidates = _stage1_search(profile)
    if not candidates:
        logger.warning("No candidates from Stage 1 — check index has data")
        return {
            "apply_now": [],
            "prepare": [],
            "apply_now_count": 0,
            "prepare_count": 0,
            "student_id": student_id,
            "student_name": profile.get("name", "")
        }

    matches     = _stage2_reasoning(profile, candidates)
    matched     = [
        m for m in matches
        if m.get("qualifies") is True and str(m.get("match_strength", "")).lower() == "strong"
    ]
    result      = _sort_and_package(matched)
    result["student_id"]   = student_id
    result["student_name"] = profile.get("name", "")
    return result


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

