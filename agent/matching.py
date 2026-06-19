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

    # GPA: no requirement, or requirement student meets
    if gpa and gpa > 0:
        filters.append(f"(gpa_requirement eq null or gpa_requirement le {gpa + 0.5})")
        # +0.5 to catch near misses — Stage 2 will filter more precisely

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

    # Build compact scholarship list for the prompt
    scholarship_list = []
    for c in candidates:
        scholarship_list.append({
            "id":               c.get("id"),
            "name":             c.get("name"),
            "faculty":          c.get("faculty"),
            "level":            c.get("level"),
            "year_of_study":    c.get("year_of_study"),
            "nationality":      c.get("nationality"),
            "gpa_requirement":  c.get("gpa_requirement"),
            "financial_need":   c.get("financial_need"),
            "merit_based":      c.get("merit_based"),
            "is_entrance":      c.get("is_entrance"),
            "is_enrichment":    c.get("is_enrichment"),
            "amount":           f"{c.get('amount', '')} {c.get('currency', 'HKD')}",
            "provider":         c.get("provider"),
            "is_open":          c.get("is_open", False),
            "deadline_raw":     c.get("deadline_raw", ""),
            "deadline_iso":     c.get("deadline_iso"),
            "application_method": c.get("application_method", ""),
            "eligibility_raw":  c.get("eligibility_raw", "")[:400],  # cap to save tokens
        })

    prompt = PROMPT_TEMPLATE.format(
        student_profile=json.dumps(profile_summary, indent=2, ensure_ascii=False),
        scholarship_candidates=json.dumps(scholarship_list, indent=2, ensure_ascii=False)
    )

    try:
        response = openai_client.chat.completions.create(
            model=DEPLOYMENT,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=6000,
            temperature=0.1  # low temperature for consistent structured output
        )
        raw = response.choices[0].message.content

        # The prompt asks for a JSON array but response_format forces an object
        # Wrap handling for both cases
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            results = parsed
        elif isinstance(parsed, dict):
            # GPT sometimes wraps in {"results": [...]} or {"scholarships": [...]}
            results = next(
                (v for v in parsed.values() if isinstance(v, list)),
                []
            )
        else:
            results = []

        logger.info(f"Stage 2: {len(results)} matches/near-misses returned")
        return results

    except json.JSONDecodeError as e:
        logger.error(f"Stage 2 JSON parse error: {e}")
        logger.error(f"Raw response was: '{raw[:500]}'")
        return []
    except Exception as e:
        logger.error(f"Stage 2 OpenAI error: {e}")
        return []


# ---------------------------------------------------------------------------
# Stage 3 — Sort and package
# ---------------------------------------------------------------------------

def _sort_and_package(matches: list, near_misses: list) -> dict:
    from datetime import date

    apply_now = []
    prepare   = []

    # Separate open vs not open from full matches
    for m in matches:
        if m.get("is_open"):
            apply_now.append(m)
        else:
            m["tier"] = "eligible_not_open"
            prepare.append(m)

    # Near misses always go into prepare with their gap info
    for m in near_misses:
        m["tier"] = "near_miss"
        prepare.append(m)

    # Sort apply_now: deadline ascending then strength
    strength_order = {"strong": 0, "partial": 1}
    apply_now.sort(key=lambda m: (
        m.get("deadline_iso") or "9999-12-31",
        strength_order.get(m.get("match_strength", "partial"), 1)
    ))

    # Sort prepare: strength first, then near_miss after eligible_not_open
    tier_order = {"eligible_not_open": 0, "near_miss": 1}
    prepare.sort(key=lambda m: (
        tier_order.get(m.get("tier", "eligible_not_open"), 0),
        strength_order.get(m.get("match_strength", "partial"), 1)
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
    matched     = [m for m in matches if m.get("match_strength") in ("strong", "partial")]
    near_misses = [m for m in matches if m.get("match_strength") == "near_miss"]
    result      = _sort_and_package(matched, near_misses)
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
        print("TIER 2 — PREPARE (opens later this year + near misses)")
        print("="*60)
        for m in result["prepare"]:
            tier_label = "NEAR MISS" if m.get("tier") == "near_miss" else "ELIGIBLE"
            print(f"\n  [{tier_label}] {m['name']}")
            print(f"  Strength : {m.get('match_strength','near_miss')}")
            print(f"  Deadline : {m.get('deadline_raw','N/A')}")
            if m.get("reason"):
                print(f"  Reason   : {m['reason']}")
            if m.get("gap"):
                print(f"  Gap      : {m['gap']}")
            if m.get("application_notes"):
                print(f"  Strengthen: {m['application_notes']}")

