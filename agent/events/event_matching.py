"""
agent/events/event_matching.py

Two-stage event personalization (mirrors scholarship matching):
  Stage 1 — keyword / faculty / year pre-filter on raw scraped posts
  Stage 2 — GPT-4o reasoning over extracted event candidates
"""

import json
import logging
import os
import re
from copy import deepcopy

from dotenv import load_dotenv
from openai import AzureOpenAI

from agent.classifier import build_profile_keywords
from agent.events.event_extractor import extract_events
from agent.events.event_filters import filter_open_events
from agent.profile import get_profile

load_dotenv()
logger = logging.getLogger(__name__)

openai_client = AzureOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version="2024-12-01-preview",
)
DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "..", "prompts", "event_matching.txt")
with open(PROMPT_PATH) as handle:
    PROMPT_TEMPLATE = handle.read()

STAGE1_LIMIT = 15
MAX_RESULTS = 8

GENERIC_EVENT_TERMS = frozenset({
    "workshop", "seminar", "talk", "career fair", "networking", "opportunity",
    "event", "lecture", "webinar", "info session", "briefing",
})

_GRADUATE_ONLY_PHRASES = (
    "graduate programme", "graduate program", "grad programme", "grad program",
    "final year undergraduate", "final-year undergraduate", "final year undergraduates",
    "fresh graduate", "fresh graduates", "penultimate year", "graduating class",
    "campus hire", "graduate intake", "graduate recruitment",
)


def _normalize_year_token(value: str) -> str:
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
    if token in {"master", "masters", "postgraduate", "pg", "mphil", "phd"}:
        return token
    match = re.search(r"\b([1-4])\b", token)
    if match and "year" in token:
        return match.group(1)
    if token.isdigit() and token in {"1", "2", "3", "4"}:
        return token
    return token


def _student_is_early_undergrad(student_year: str) -> bool:
    return _normalize_year_token(student_year) in {"1", "2", "3"}


def _event_text_blob(event: dict) -> str:
    return " ".join(
        str(event.get(key) or "")
        for key in ("title", "summary", "eligibility", "organiser", "type")
    ).lower()


def _is_graduate_or_final_year_event(event: dict) -> bool:
    blob = _event_text_blob(event)
    return any(phrase in blob for phrase in _GRADUATE_ONLY_PHRASES)


def _event_year_tags(event: dict) -> set[str]:
    tags = event.get("year_relevant") or event.get("year_tags") or []
    if isinstance(tags, str):
        tags = [tags]
    normalized = {_normalize_year_token(tag) for tag in tags if str(tag).strip()}
    return {tag for tag in normalized if tag}


def _event_year_eligible(event: dict, profile: dict) -> bool:
    student_year = _normalize_year_token(_student_year(profile))
    if not student_year:
        return True

    tags = _event_year_tags(event)
    if _student_is_early_undergrad(student_year) and _is_graduate_or_final_year_event(event):
        return False

    if not tags or tags == {"all"}:
        return True

    specific = tags - {"all"}
    if not specific:
        return True

    if student_year in specific:
        return True

    if student_year in {"1", "2", "3", "4"} and specific.intersection({"4", "master", "masters", "postgraduate", "pg"}):
        return student_year == "4" and "4" in specific

    return False


def _reason_is_profile_grounded(reason: str, profile: dict) -> bool:
    reason_lower = str(reason or "").strip().lower()
    if not reason_lower:
        return False

    interests = [str(i).strip().lower() for i in (profile.get("interests") or []) if str(i).strip()]
    activities = [str(a).strip().lower() for a in (profile.get("activities") or []) if str(a).strip()]
    profile_terms = interests + activities

    weak_only_phrases = (
        "open to hku students in",
        "relevant to ",
        "listed in this week's campus events",
    )
    if any(phrase in reason_lower for phrase in weak_only_phrases):
        return any(term in reason_lower for term in profile_terms)

    if any(marker in reason_lower for marker in ("matches your interests", "related to your activity", "related to your cv")):
        return True
    return any(term in reason_lower for term in profile_terms)


def _student_faculty(profile: dict) -> str:
    academic = profile.get("academic") or {}
    return str(academic.get("faculty") or profile.get("faculty") or "").strip().lower()


def _student_year(profile: dict) -> str:
    academic = profile.get("academic") or {}
    year = academic.get("year_of_study") or profile.get("year") or ""
    return str(year).strip().lower()


def _faculty_matches(post: dict, student_faculty: str) -> bool:
    tags = post.get("faculty_tags") or ["all"]
    normalized = {str(tag).strip().lower() for tag in tags if str(tag).strip()}
    if not normalized or "all" in normalized:
        return True
    if not student_faculty:
        return True
    for tag in normalized:
        if tag in student_faculty or student_faculty in tag:
            return True
    return False


def _year_matches(post: dict, student_year: str) -> bool:
    tags = post.get("year_tags") or ["all"]
    normalized = {_normalize_year_token(tag) for tag in tags if str(tag).strip()}
    normalized = {tag for tag in normalized if tag}
    if not normalized or normalized == {"all"}:
        return True
    if not student_year:
        return True

    student = _normalize_year_token(student_year)
    specific = normalized - {"all"}
    if not specific:
        return True
    if student in specific:
        return True
    if student in {"1", "2", "3"} and specific.intersection({"4", "master", "masters", "postgraduate", "pg"}):
        return False
    return False


def _post_search_text(post: dict) -> str:
    parts = [
        post.get("raw_text", ""),
        post.get("poster", ""),
        " ".join(post.get("keywords") or []),
    ]
    return " ".join(parts).lower()


def _profile_match_terms(profile: dict) -> list[str]:
    """Terms derived from explicit profile fields (not generic student vocabulary)."""
    terms = []
    for interest in profile.get("interests") or []:
        token = str(interest).strip().lower()
        if token and token not in GENERIC_EVENT_TERMS:
            terms.append(token)
    for activity in profile.get("activities") or []:
        token = str(activity).strip().lower()
        if token and len(token) > 3:
            terms.append(token)
    cv_text = str(profile.get("cv_text") or "").lower()
    if cv_text:
        for chunk in re.split(r"[,;\n•\-]+", cv_text):
            token = chunk.strip()
            if len(token) > 4 and token not in GENERIC_EVENT_TERMS:
                terms.append(token[:80])
    academic = profile.get("academic") or {}
    programme = str(academic.get("programme") or profile.get("programme") or "").strip().lower()
    if programme:
        terms.append(programme)
    faculty = _student_faculty(profile)
    if faculty:
        terms.append(faculty)
    keywords = build_profile_keywords(profile or {}, include_generic=False)
    for keyword in keywords:
        key = keyword.lower()
        if key not in GENERIC_EVENT_TERMS and len(key) > 2:
            terms.append(key)
    seen = set()
    unique = []
    for term in terms:
        if term not in seen:
            seen.add(term)
            unique.append(term)
    return unique[:30]


def _score_post(post: dict, profile: dict, profile_keywords: list[str]) -> int:
    if post.get("is_noise"):
        return -1

    text = _post_search_text(post)
    post_keywords = {str(k).strip().lower() for k in (post.get("keywords") or []) if str(k).strip()}
    score = 0

    hits = []
    for keyword in profile_keywords:
        key = keyword.lower()
        if key in GENERIC_EVENT_TERMS:
            continue
        if key in text or key in post_keywords:
            hits.append(key)
    score += min(len(set(hits)) * 2, 12)

    for interest in profile.get("interests") or []:
        token = str(interest).strip().lower()
        if token and token not in GENERIC_EVENT_TERMS and token in text:
            score += 3

    if _faculty_matches(post, _student_faculty(profile)):
        score += 2
    else:
        score -= 4

    if _year_matches(post, _student_year(profile)):
        score += 2
    else:
        score -= 8

    if _student_is_early_undergrad(_student_year(profile)) and _is_graduate_or_final_year_event(
        {"title": post.get("raw_text", ""), "summary": post.get("raw_text", ""), "eligibility": post.get("raw_text", "")}
    ):
        score -= 10

    return score


def stage1_filter_posts(profile: dict, posts: list, limit: int = STAGE1_LIMIT) -> list:
    """Keyword and profile pre-filter — returns top candidate raw posts."""
    profile_keywords = build_profile_keywords(profile or {}, include_generic=False)
    scored = []
    for post in posts or []:
        score = _score_post(post, profile or {}, profile_keywords)
        if score >= 0:
            scored.append((score, post))

    scored.sort(key=lambda item: item[0], reverse=True)
    strong = [post for score, post in scored if score >= 4][:limit]
    if len(strong) >= 5:
        return strong

    fallback = [post for _, post in scored[:limit]]
    return fallback or list(posts or [])[:limit]


def _compact_profile(profile: dict) -> dict:
    academic = profile.get("academic") or {}
    cv_text = str(profile.get("cv_text") or "").strip()
    return {
        "name": profile.get("name", ""),
        "faculty": academic.get("faculty") or profile.get("faculty", ""),
        "programme": academic.get("programme") or profile.get("programme", ""),
        "year_of_study": academic.get("year_of_study") or profile.get("year", ""),
        "interests": profile.get("interests") or [],
        "activities": profile.get("activities") or [],
        "courses": profile.get("courses") or [],
        "cv_excerpt": cv_text[:1200] if cv_text else "",
    }


def _compact_event(event: dict) -> dict:
    return {
        "source_id": event.get("source_id") or event.get("id"),
        "title": event.get("title"),
        "type": event.get("type"),
        "organiser": event.get("organiser"),
        "deadline": event.get("deadline"),
        "eligibility": event.get("eligibility"),
        "faculty_relevant": event.get("faculty_relevant"),
        "year_relevant": event.get("year_relevant"),
        "skills_required": event.get("skills_required"),
        "summary": event.get("summary"),
        "source": event.get("source"),
    }


def _passes_profile_fit(event: dict, profile: dict, *, min_term_hits: int = 1) -> bool:
    if not _event_year_eligible(event, profile):
        return False
    blob = _event_text_blob(event)
    match_terms = _profile_match_terms(profile)
    hits = sum(
        1 for term in match_terms
        if term.lower() in blob and term.lower() not in GENERIC_EVENT_TERMS
    )
    interest_hits = _meaningful_term_hits(
        [str(i) for i in (profile.get("interests") or [])],
        blob,
    )
    activity_hits = _meaningful_term_hits(
        [str(a) for a in (profile.get("activities") or [])],
        blob,
    )
    return hits >= min_term_hits or bool(interest_hits) or bool(activity_hits)


def stage2_reasoning(profile: dict, events: list) -> list[dict]:
    """LLM reasoning — keep strong personalized matches only."""
    if not events:
        return []

    prompt = PROMPT_TEMPLATE.format(
        student_profile=json.dumps(_compact_profile(profile), ensure_ascii=False, indent=2),
        event_candidates=json.dumps([_compact_event(event) for event in events], ensure_ascii=False, indent=2),
    )

    try:
        response = openai_client.chat.completions.create(
            model=DEPLOYMENT,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=2000,
            temperature=0.1,
        )
        raw = response.choices[0].message.content or "[]"
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            parsed = next((value for value in parsed.values() if isinstance(value, list)), [])
        if not isinstance(parsed, list):
            return _fallback_rank(profile, events)

        by_id = {(event.get("source_id") or event.get("id")): event for event in events}
        matched = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            if item.get("is_relevant") is not True:
                continue
            if str(item.get("match_strength", "")).lower() != "strong":
                continue
            source_id = item.get("source_id")
            event = by_id.get(source_id)
            if not event:
                continue
            enriched = deepcopy(event)
            enriched["match_strength"] = "strong"
            reason = str(item.get("reason") or "").strip()
            if not _passes_profile_fit(event, profile):
                continue
            if not _reason_is_profile_grounded(reason, profile):
                reason = _fallback_match_reason(event, profile, _event_text_blob(event))
                if not _reason_is_profile_grounded(reason, profile):
                    continue
            enriched["match_reason"] = reason
            matched.append(enriched)

        if matched:
            return matched[:MAX_RESULTS]
        return _fallback_rank(profile, events)

    except Exception as exc:
        logger.warning("Event matching stage 2 failed: %s", exc)
        return _fallback_rank(profile, events)


def _meaningful_term_hits(terms: list[str], blob: str) -> list[str]:
    hits = []
    for term in terms:
        key = term.lower().strip()
        if len(key) < 3 or key in GENERIC_EVENT_TERMS:
            continue
        if key in blob:
            hits.append(term)
    return sorted(set(hits), key=len, reverse=True)[:3]


def _fallback_match_reason(event: dict, profile: dict, blob: str) -> str:
    interests = [str(i).strip() for i in (profile.get("interests") or []) if str(i).strip()]
    interest_hits = _meaningful_term_hits(interests, blob)
    if interest_hits:
        return f"Matches your interests: {', '.join(interest_hits[:3])}."

    activity_hits = _meaningful_term_hits(
        [str(a) for a in (profile.get("activities") or [])],
        blob,
    )
    if activity_hits:
        return f"Related to your activity: {activity_hits[0][:80]}."

    cv_hits = _meaningful_term_hits(_profile_match_terms(profile), blob)
    if cv_hits:
        return f"Related to your CV/experience: {cv_hits[0][:80]}."

    return ""


def _fallback_rank(profile: dict, events: list) -> list:
    """Heuristic fallback when LLM is unavailable."""
    ranked = []
    for event in events:
        if not _passes_profile_fit(event, profile):
            continue
        blob = _event_text_blob(event)
        match_terms = _profile_match_terms(profile)
        hits = sum(
            1 for term in match_terms
            if term.lower() in blob and term.lower() not in GENERIC_EVENT_TERMS
        )
        reason = _fallback_match_reason(event, profile, blob)
        if not reason or not _reason_is_profile_grounded(reason, profile):
            continue
        copy = deepcopy(event)
        copy["match_strength"] = "strong"
        copy["match_reason"] = reason
        ranked.append((hits, copy))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [event for _, event in ranked[:MAX_RESULTS]]


def match_events_for_student(profile: dict, posts: list) -> list:
    """Full pipeline: filter posts → extract → reason."""
    candidates = stage1_filter_posts(profile, posts)
    logger.info("Event stage 1: %s candidates from %s posts", len(candidates), len(posts or []))

    extracted = extract_events(candidates)
    if not extracted:
        return []

    matched = stage2_reasoning(profile, extracted)
    logger.info("Event stage 2: %s personalized matches from %s extracted", len(matched), len(extracted))
    return filter_open_events(matched)


def run_event_matching(student_id: str) -> list:
    """Entry point used by digest — loads mocks and personalizes."""
    from agent.events.mock_linkedin import get_mock_linkedin_posts
    from agent.events.mock_xiaohongshu import get_mock_xhs_posts

    profile = get_profile(student_id) or {}
    posts = get_mock_linkedin_posts() + get_mock_xhs_posts()
    return match_events_for_student(profile, posts)
