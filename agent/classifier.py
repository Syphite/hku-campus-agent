import json
import os
import re
from dotenv import load_dotenv
from openai import AzureOpenAI

load_dotenv()

# Azure OpenAI setup
openai_client = AzureOpenAI(
    azure_endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT"),
    api_key=os.environ.get("AZURE_OPENAI_API_KEY"),
    api_version="2024-12-01-preview"
)
DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
HAS_LLM = True

URGENT_TERMS = [
    "deadline", "due tomorrow", "due today", "final exam", "exam", "submission",
    "urgent", "important admin", "registration", "last chance", "tomorrow 11:59pm",
    "library books due", "fee payment", "course add drop"
]

NOISE_TERMS = [
    "promo", "promotion", "sale", "discount", "off ", "buy now", "limited time",
    "unsubscribe", "free delivery", "voucher", "deal", "coupon", "advertisement"
]

GENERAL_STUDENT_TERMS = [
    "career fair", "internship", "networking", "workshop", "seminar", "talk",
    "groupmates", "project group", "hackathon", "scholarship", "research",
    "mentorship", "career", "resume", "cv", "interview", "opportunity"
]

MAJOR_COURSE_HINTS = {
    "computer science": ["programming", "algorithms", "data structures", "ai", "machine learning", "software engineering", "database", "systems"],
    "information systems": ["database", "business analytics", "systems analysis", "project management", "data", "cloud"],
    "business": ["marketing", "finance", "accounting", "consulting", "strategy", "analytics", "entrepreneurship"],
    "economics": ["economics", "statistics", "data analysis", "finance", "policy", "research"],
    "engineering": ["math", "physics", "design", "lab", "systems", "electronics", "mechanical", "civil"],
    "law": ["law", "legal", "case", "moot", "policy", "regulation"],
    "medicine": ["clinical", "health", "bio", "anatomy", "research", "lab"],
    "arts": ["writing", "literature", "history", "culture", "media", "communication"],
}

YEAR_HINTS = {
    "year 1": ["intro", "foundation", "orientation", "beginner", "101"],
    "year 2": ["core", "intermediate", "internship", "team project"],
    "year 3": ["advanced", "internship", "research", "capstone", "exchange"],
    "year 4": ["graduate", "job", "career", "final year", "capstone", "interview"],
    "master": ["research", "seminar", "career", "networking", "industry"],
    "phd": ["research", "paper", "seminar", "conference", "workshop"],
}

def _normalize_list(values):
    if not values:
        return []
    out = []
    for v in values:
        v = str(v).strip()
        if v:
            out.append(v)
    return out

def suggested_course_keywords(profile):
    academic = profile.get("academic", {})
    major = str(profile.get("major") or academic.get("programme", "")).strip().lower()
    year = str(profile.get("year") or academic.get("year_of_study", "")).strip().lower()
    hints = []

    for key, values in MAJOR_COURSE_HINTS.items():
        if key in major:
            hints.extend(values)

    for key, values in YEAR_HINTS.items():
        if key in year:
            hints.extend(values)

    # Keep unique order
    seen = set()
    result = []
    for x in hints:
        if x not in seen:
            seen.add(x)
            result.append(x)

    return result[:12]

def build_profile_keywords(profile, include_generic=True):
    academic = profile.get("academic", {})
    interests = _normalize_list(profile.get("interests", []))
    courses = _normalize_list(profile.get("courses", []))
    major = str(profile.get("major") or academic.get("programme", "")).strip().lower()
    keywords = set()

    for item in interests + courses:
        keywords.add(item.lower())

    for key, values in MAJOR_COURSE_HINTS.items():
        if key in major:
            keywords.update(values)

    keywords.update(suggested_course_keywords(profile))
    if include_generic:
        keywords.update(GENERAL_STUDENT_TERMS)

    return sorted(keywords)

def _keyword_in_text(keyword: str, text: str) -> bool:
    """Match profile keywords without substring false positives (e.g. ai in Catholic)."""
    token = str(keyword or "").strip().lower()
    if not token:
        return False
    if len(token) <= 4 or token in {"ai", "lab", "cv", "pg", "data", "bio", "case"}:
        return bool(re.search(rf"\b{re.escape(token)}\b", text, re.IGNORECASE))
    return token in text.lower()


def _profile_keyword_hits(keywords: list[str], text: str) -> list[str]:
    return [keyword for keyword in keywords if _keyword_in_text(keyword, text)]


def _post_validate_classification(result: dict, subject: str, preview: str, profile: dict) -> dict:
    """Downgrade relevant labels when the reason cites terms not present in the email."""
    if result.get("label") != "relevant":
        return result

    text = f"{subject} {preview}".lower()
    interests = [str(item).strip() for item in (profile.get("interests") or []) if str(item).strip()]
    courses = [str(item).strip() for item in (profile.get("courses") or []) if str(item).strip()]
    interest_hits = _profile_keyword_hits([item.lower() for item in interests], text)
    course_hits = _profile_keyword_hits([item.lower() for item in courses], text)

    if interest_hits:
        result["reason"] = f"Matches your interests: {', '.join(interest_hits[:3])}."
        return result
    if course_hits:
        result["reason"] = f"Related to your courses: {', '.join(course_hits[:3])}."
        return result

    reason = str(result.get("reason") or "")
    cited_terms = []
    for prefix in ("strong profile match:", "matches your interests:", "related to your courses:"):
        if prefix in reason.lower():
            tail = reason.split(":", 1)[-1]
            cited_terms = [part.strip() for part in tail.split(",") if part.strip()]
            break
    verified = _profile_keyword_hits([term.lower() for term in cited_terms], text)
    if len(verified) >= 2:
        result["reason"] = f"Strong profile match: {', '.join(verified[:4])}."
        return result
    if len(verified) == 1:
        result["label"] = "ambiguous"
        result["reason"] = f"Possible match ({verified[0]}) — needs a quick review."
        return result

    result["label"] = "ambiguous"
    result["reason"] = "Campus email without a verified match to your profile — review recommended."
    return result


def heuristic_classify(subject, body_preview, sender, profile=None):
    profile = profile or {}
    text = f"{subject} {body_preview} {sender}".lower()
    profile_keywords = build_profile_keywords(profile, include_generic=False)
    broad_keywords = build_profile_keywords(profile, include_generic=True)

    urgent_hits = [k for k in URGENT_TERMS if k in text]
    noise_hits = [k for k in NOISE_TERMS if k in text]
    profile_hits = _profile_keyword_hits(profile_keywords, text)
    broad_hits = _profile_keyword_hits(broad_keywords, text)
    broad_hits = [hit for hit in broad_hits if hit not in profile_hits]

    sender_lower = sender.lower()
    is_asso_forum = "asso_forum" in sender_lower or "asso_forum" in text
    is_university_sender = "@hku.hk" in sender_lower or "hku" in sender_lower

    if urgent_hits and (profile_hits or is_university_sender):
        return {
            "label": "urgent",
            "reason": (
                f"Urgent email with profile or HKU context: "
                f"{', '.join(urgent_hits[:3])}."
            ),
            "decisive": True,
        }

    if noise_hits and not profile_hits and not is_university_sender and not is_asso_forum:
        return {
            "label": "noise",
            "reason": f"Promotional/noise language detected: {', '.join(noise_hits[:3])}.",
            "decisive": True,
        }

    if len(profile_hits) >= 2:
        return {
            "label": "relevant",
            "reason": f"Strong profile match: {', '.join(profile_hits[:4])}.",
            "decisive": True,
        }

    if profile_hits and urgent_hits:
        return {
            "label": "relevant",
            "reason": f"Matches your interests and mentions timing: {', '.join(profile_hits[:3])}.",
            "decisive": True,
        }

    if profile_hits:
        return {
            "label": "ambiguous",
            "reason": f"Possible profile match ({', '.join(profile_hits[:3])}) — needs a quick review.",
            "decisive": False,
        }

    if is_asso_forum or is_university_sender:
        return {
            "label": "ambiguous",
            "reason": "HKU campus mail without a clear personal match — review recommended.",
            "decisive": False,
        }

    if broad_hits:
        return {
            "label": "ambiguous",
            "reason": f"General student-life topic ({', '.join(broad_hits[:3])}) — may or may not be useful.",
            "decisive": False,
        }

    if noise_hits:
        return {
            "label": "noise",
            "reason": f"Promotional language with no profile match: {', '.join(noise_hits[:3])}.",
            "decisive": True,
        }

    return {
        "label": "ambiguous",
        "reason": "No clear match to your profile — flagged for review.",
        "decisive": False,
    }


def _llm_classify(subject: str, body_preview: str, sender: str, profile: dict, heuristic: dict) -> dict:
    academic = profile.get("academic", {})
    major = str(profile.get("major") or academic.get("programme", "")).strip()
    year = str(profile.get("year") or academic.get("year_of_study", "")).strip()
    profile_text = {
        "year": year,
        "major": major,
        "interests": _normalize_list(profile.get("interests", [])),
        "courses": _normalize_list(profile.get("courses", [])),
        "course_keywords": suggested_course_keywords(profile),
    }

    prompt = f"""
You are an email triage assistant for a university student.
Classify this email into exactly one label:
- urgent: requires immediate action (deadline within days, exam, registration, payment)
- relevant: clearly useful based on the student's profile, courses, or interests (high confidence)
- noise: clear marketing/spam/unrelated promotional email (safe to archive)
- ambiguous: bulk campus mail, newsletters, generic announcements, or anything uncertain

Student profile:
{json.dumps(profile_text, ensure_ascii=False, indent=2)}

Keyword pre-check (may be incomplete):
{json.dumps({"label": heuristic.get("label"), "reason": heuristic.get("reason")}, ensure_ascii=False)}

Email:
From: {sender}
Subject: {subject}
Preview: {body_preview}

Routing targets (approximate):
- Most mail should land in ambiguous or noise, not stay in the inbox.
- Use ambiguous for HKU bulk mail, ASSO_FORUM posts, club mail, and generic campus updates
  unless there is a strong, specific match to the student's profile.
- Use relevant only when the email clearly matches the student's major, courses, or interests.
- In the reason, only mention interests/courses/keywords that literally appear in the subject or preview.
- Never invent profile matches. If no explicit match appears in the email text, use ambiguous.
- Use noise only for obvious external marketing/promo with no student relevance.
- When unsure between relevant and ambiguous, choose ambiguous.
- When unsure between ambiguous and noise for @hku.hk senders, choose ambiguous.

Return JSON only with keys: label, reason.
"""

    response = openai_client.chat.completions.create(
        model=DEPLOYMENT,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=200,
        temperature=0.1,
    )
    result = json.loads(response.choices[0].message.content)
    label = str(result.get("label", "ambiguous")).lower().strip()
    if label not in {"urgent", "relevant", "noise", "ambiguous"}:
        raise ValueError(f"Invalid LLM label: {label}")
    return {"label": label, "reason": str(result.get("reason", ""))}


def classify_email(subject, body_preview, sender, profile=None):
    subject = subject or ""
    body_preview = body_preview or ""
    sender = sender or ""
    profile = profile or {}

    if not subject and not body_preview:
        return {"label": "noise", "reason": "Empty email, automatically ignored."}

    heuristic = heuristic_classify(subject, body_preview, sender, profile)
    if heuristic.get("decisive"):
        return _post_validate_classification(
            {"label": heuristic["label"], "reason": heuristic["reason"]},
            subject,
            body_preview,
            profile,
        )

    if HAS_LLM:
        try:
            return _post_validate_classification(
                _llm_classify(subject, body_preview, sender, profile, heuristic),
                subject,
                body_preview,
                profile,
            )
        except Exception:
            label = heuristic.get("label") or "ambiguous"
            if label not in {"urgent", "relevant", "noise", "ambiguous"}:
                label = "ambiguous"
            return {"label": label, "reason": heuristic.get("reason", "")}

    label = heuristic.get("label") or "ambiguous"
    return {"label": label, "reason": heuristic.get("reason", "")}
