"""
agent/learner.py
Extracts semantic learning signals from explicit user feedback.
"""

import json
import logging
import os
from typing import Optional

from dotenv import load_dotenv
from openai import AzureOpenAI

load_dotenv()
logger = logging.getLogger(__name__)


def _get_openai_client() -> Optional[AzureOpenAI]:
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")
    if not endpoint or not api_key:
        logger.warning("Azure OpenAI is not configured; skipping semantic learning")
        return None

    return AzureOpenAI(
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version="2024-12-01-preview"
    )


def _parse_interests(raw: str) -> list:
    raw = (raw or "").strip()
    if not raw:
        return []

    if "```" in raw:
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    parsed = json.loads(raw)
    if isinstance(parsed, list):
        values = parsed
    elif isinstance(parsed, dict):
        values = parsed.get("interests") or parsed.get("topics") or []
    else:
        values = []

    interests = []
    seen = set()
    for value in values:
        text = str(value).strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            interests.append(text)
    return interests[:3]


def extract_learned_interests(email_subject: str, email_body: str) -> list:
    """
    Extract 1-3 specific interest keywords from an email the user restored.
    Returns an empty list if extraction fails.
    """
    client = _get_openai_client()
    if not client:
        return []

    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    prompt = f"""
You are helping a university email assistant learn from explicit user feedback.

The student moved this archived email back to their inbox, so it likely reflects
something they care about. Extract 1-3 specific interest keywords or topics from
the email. Prefer concrete topics such as "Song Dynasty History", "AI Hackathon",
"Public Health Internship", or "FinTech Case Competition".

Return JSON only in this exact shape:
{{"interests": ["topic 1", "topic 2"]}}

Email subject:
{email_subject or ""}

Email preview/body:
{email_body or ""}
"""

    try:
        response = client.chat.completions.create(
            model=deployment,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=200,
            temperature=0.1
        )
        raw = response.choices[0].message.content
        return _parse_interests(raw)
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning(f"Could not parse learned interests: {e}")
        return []
    except Exception as e:
        logger.error(f"Interest extraction failed: {e}")
        return []
