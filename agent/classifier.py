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

def build_profile_keywords(profile):
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
    keywords.update(GENERAL_STUDENT_TERMS)

    return sorted(keywords)

def heuristic_classify(subject, body_preview, sender, profile=None):
    profile = profile or {}
    text = f"{subject} {body_preview} {sender}".lower()
    profile_keywords = build_profile_keywords(profile)
    
    urgent_hits = [k for k in URGENT_TERMS if k in text]
    noise_hits = [k for k in NOISE_TERMS if k in text]
    profile_hits = [k for k in profile_keywords if k in text]

    sender_lower = sender.lower()
    is_asso_forum = "asso_forum" in sender_lower or "asso_forum" in text
    is_university_sender = "@hku.hk" in sender_lower or "hku" in sender_lower

    # Special handling for ASSO_FORUM
    if is_asso_forum:
        if profile_hits:
            return {
                "label": "relevant",
                "reason": f"ASSO_FORUM post matches your profile keywords: {', '.join(profile_hits[:4])}.",
                "decisive": len(profile_hits) >= 2,
            }
        return {
            "label": "ambiguous",
            "reason": "ASSO_FORUM post may be useful, but it does not clearly match your interests or major.",
            "decisive": False,
        }

    if urgent_hits:
        return {
            "label": "urgent",
            "reason": (
                f"Urgent email with matches: {', '.join(profile_hits[:3]) or 'student/admin context'}."
                if profile_hits or is_university_sender
                else f"Contains urgent terms: {', '.join(urgent_hits[:3])}."
            ),
            "decisive": True,
        }

    if noise_hits and not profile_hits:
        return {
            "label": "noise",
            "reason": f"Promotional/noise language detected: {', '.join(noise_hits[:3])}.",
            "decisive": True,
        }

    if profile_hits:
        return {
            "label": "relevant",
            "reason": f"Matches your interests/major/year keywords: {', '.join(profile_hits[:4])}.",
            "decisive": len(profile_hits) >= 2,
        }

    if is_university_sender:
        return {
            "label": "ambiguous",
            "reason": "University sender, but the content is not clearly important for your profile.",
            "decisive": False,
        }

    return {
        "label": "noise",
        "reason": "No strong match to your profile or student priorities.",
        "decisive": bool(noise_hits),
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
- urgent: requires immediate action
- relevant: useful for the student based on profile or student life
- noise: promotional, spam, irrelevant mass email
- ambiguous: unclear, needs human judgment

Student profile:
{json.dumps(profile_text, ensure_ascii=False, indent=2)}

Keyword pre-check (may be incomplete):
{json.dumps({"label": heuristic.get("label"), "reason": heuristic.get("reason")}, ensure_ascii=False)}

Email:
From: {sender}
Subject: {subject}
Preview: {body_preview}

Rules:
- Use the student profile strongly.
- ASSO_FORUM posts are often useful, but only mark them relevant when they match interests, major, year, or likely courses.
- If the email is marketing/promo and not relevant to the profile, mark it noise.
- Return JSON only with keys: label, reason.
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
        return {"label": heuristic["label"], "reason": heuristic["reason"]}

    if HAS_LLM:
        try:
            return _llm_classify(subject, body_preview, sender, profile, heuristic)
        except Exception:
            return {"label": heuristic["label"], "reason": heuristic["reason"]}

    return {"label": heuristic["label"], "reason": heuristic["reason"]}
