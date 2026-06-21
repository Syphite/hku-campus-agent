"""Natural-language intent routing for the campus agent."""

from __future__ import annotations

import json
import logging
import os
import re

from dotenv import load_dotenv
from openai import AzureOpenAI

load_dotenv()
logger = logging.getLogger(__name__)

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "intent_router.txt")
with open(PROMPT_PATH, encoding="utf-8") as handle:
    PROMPT_TEMPLATE = handle.read()

openai_client = AzureOpenAI(
    azure_endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT", ""),
    api_key=os.environ.get("AZURE_OPENAI_API_KEY", ""),
    api_version="2024-12-01-preview",
)
DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

VALID_INTENTS = {
    "digest",
    "scholarships",
    "events",
    "inbox",
    "apply_scholarship",
    "profile_update",
    "help",
    "unknown",
}

EXACT_COMMANDS = {
    "digest": {"digest", "update", "show me", "what's new", "whats new", "opportunities"},
    "scholarships": {
        "scholarship", "scholarships", "show scholarships", "browse scholarships",
        "show me scholarships", "scholarship matches",
    },
    "events": {"events", "show events", "competitions", "show me events", "event matches"},
    "inbox": {"inbox", "show inbox", "check inbox", "my inbox"},
    "help": {"help", "commands", "what can you do"},
}

APPLY_PATTERNS = (
    re.compile(r"\b(?:apply|start|begin|help me apply|application)\b.*\b(?:for|to)\b\s+(.+)", re.I),
    re.compile(r"\b(?:apply|start)\b\s+(.+?)\s+scholarship", re.I),
)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _keyword_route(text: str) -> dict | None:
    normalized = _normalize_text(text)
    if not normalized:
        return None

    for intent, commands in EXACT_COMMANDS.items():
        if normalized in commands:
            return {"intent": intent, "scholarship_query": "", "confidence": "high", "source": "keyword"}

    focus_terms = (
        "what should i focus", "what do i need to do", "plan my week",
        "priorities", "anything urgent", "what's urgent", "whats urgent",
        "brief me", "catch me up", "my week", "weekly update",
    )
    if any(term in normalized for term in focus_terms):
        return {"intent": "digest", "scholarship_query": "", "confidence": "high", "source": "keyword"}

    for pattern in APPLY_PATTERNS:
        match = pattern.search(text or "")
        if match:
            query = match.group(1).strip(" .!?")
            if query and len(query) >= 3:
                return {
                    "intent": "apply_scholarship",
                    "scholarship_query": query,
                    "confidence": "high",
                    "source": "keyword",
                }

    profile_terms = (
        "change my", "update my", "set my", "add to my", "remove from my",
        "modify my", "make my", "add class", "add course", "add timetable",
        "add to calendar", "calendar event",
    )
    if any(term in normalized for term in profile_terms):
        return {"intent": "profile_update", "scholarship_query": "", "confidence": "high", "source": "keyword"}

    return None


def _parse_router_json(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Router response must be a JSON object")
    return parsed


def _llm_route(text: str) -> dict:
    prompt = PROMPT_TEMPLATE.format(user_message=text)
    response = openai_client.chat.completions.create(
        model=DEPLOYMENT,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=250,
        temperature=0.0,
    )
    parsed = _parse_router_json(response.choices[0].message.content or "")
    intent = str(parsed.get("intent") or "unknown").strip().lower()
    if intent not in VALID_INTENTS:
        intent = "unknown"
    confidence = str(parsed.get("confidence") or "medium").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium"
    return {
        "intent": intent,
        "scholarship_query": str(parsed.get("scholarship_query") or "").strip(),
        "confidence": confidence,
        "agent_response": str(parsed.get("agent_response") or "").strip(),
        "source": "llm",
    }


def route_message(text: str, profile: dict | None = None) -> dict:
    """
    Route a user message to an intent.
    Uses fast keyword/heuristic matching first, then LLM for natural language.
    """
    profile = profile or {}
    if not (text or "").strip():
        return {"intent": "unknown", "scholarship_query": "", "confidence": "low", "source": "empty"}

    fast = _keyword_route(text)
    if fast:
        return fast

    if not profile.get("onboarding_complete"):
        return {"intent": "unknown", "scholarship_query": "", "confidence": "low", "source": "pre_onboarding"}

    try:
        if not os.environ.get("AZURE_OPENAI_ENDPOINT") or not os.environ.get("AZURE_OPENAI_API_KEY"):
            return {"intent": "unknown", "scholarship_query": "", "confidence": "low", "source": "no_llm"}
        return _llm_route(text)
    except Exception as exc:
        logger.warning("Intent router LLM failed: %s", exc)
        return {"intent": "unknown", "scholarship_query": "", "confidence": "low", "source": "error"}
