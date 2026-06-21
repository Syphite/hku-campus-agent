"""
agent/events/event_matching.py

Two-stage event personalization (mirrors scholarship matching):
  Stage 1 — keyword / faculty / year pre-filter on raw scraped posts
  Stage 2 — GPT-4o reasoning over extracted event candidates
"""

import json
import logging
import os
from copy import deepcopy

from dotenv import load_dotenv
from openai import AzureOpenAI

from agent.classifier import build_profile_keywords
from agent.events.event_extractor import extract_events
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
    normalized = {str(tag).strip().lower() for tag in tags if str(tag).strip()}
    if not normalized or "all" in normalized:
        return True
    if not student_year:
        return True
    return student_year in normalized


def _post_search_text(post: dict) -> str:
    parts = [
        post.get("raw_text", ""),
        post.get("poster", ""),
        " ".join(post.get("keywords") or []),
    ]
    return " ".join(parts).lower()


def _score_post(post: dict, profile: dict, profile_keywords: list[str]) -> int:
    if post.get("is_noise"):
        return -1

    text = _post_search_text(post)
    post_keywords = {str(k).strip().lower() for k in (post.get("keywords") or []) if str(k).strip()}
    score = 0

    hits = []
    for keyword in profile_keywords:
        key = keyword.lower()
        if key in text or key in post_keywords:
            hits.append(key)
    score += min(len(set(hits)) * 2, 12)

    for interest in profile.get("interests") or []:
        token = str(interest).strip().lower()
        if token and token in text:
            score += 3

    if _faculty_matches(post, _student_faculty(profile)):
        score += 3
    else:
        score -= 4

    if _year_matches(post, _student_year(profile)):
        score += 2
    else:
        score -= 3

    return score


def stage1_filter_posts(profile: dict, posts: list, limit: int = STAGE1_LIMIT) -> list:
    """Keyword and profile pre-filter — returns top candidate raw posts."""
    profile_keywords = build_profile_keywords(profile or {})
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
    return {
        "name": profile.get("name", ""),
        "faculty": academic.get("faculty") or profile.get("faculty", ""),
        "programme": academic.get("programme") or profile.get("programme", ""),
        "year_of_study": academic.get("year_of_study") or profile.get("year", ""),
        "interests": profile.get("interests") or [],
        "courses": profile.get("courses") or [],
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
            enriched["match_reason"] = str(item.get("reason") or "").strip()
            matched.append(enriched)

        if matched:
            return matched[:MAX_RESULTS]
        return _fallback_rank(profile, events)

    except Exception as exc:
        logger.warning("Event matching stage 2 failed: %s", exc)
        return _fallback_rank(profile, events)


def _fallback_rank(profile: dict, events: list) -> list:
    """Heuristic fallback when LLM is unavailable."""
    profile_keywords = set(build_profile_keywords(profile or {}))
    ranked = []
    for event in events:
        blob = " ".join(
            str(event.get(key, "") or "")
            for key in ("title", "summary", "eligibility", "organiser", "type")
        ).lower()
        hits = sum(1 for keyword in profile_keywords if keyword in blob)
        if hits >= 1 or _faculty_matches({"faculty_tags": event.get("faculty_relevant") or ["all"]}, _student_faculty(profile)):
            copy = deepcopy(event)
            copy["match_strength"] = "strong"
            copy["match_reason"] = f"Keyword overlap with your profile ({hits} signals)."
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
    return matched


def run_event_matching(student_id: str) -> list:
    """Entry point used by digest — loads mocks and personalizes."""
    from agent.events.mock_linkedin import get_mock_linkedin_posts
    from agent.events.mock_xiaohongshu import get_mock_xhs_posts

    profile = get_profile(student_id) or {}
    posts = get_mock_linkedin_posts() + get_mock_xhs_posts()
    return match_events_for_student(profile, posts)
