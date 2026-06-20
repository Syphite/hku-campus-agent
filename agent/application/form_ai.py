"""AI analysis and conversational data helpers for application forms."""

import json
import logging
import os
from typing import Any

from dotenv import load_dotenv
from openai import AzureOpenAI

from agent.application.cell_utils import parse_llm_json

load_dotenv()
logger = logging.getLogger(__name__)

PROMPT_DIR = os.path.join(os.path.dirname(__file__), "..", "prompts")


def _load_prompt(name: str) -> str:
    with open(os.path.join(PROMPT_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _get_openai_client() -> AzureOpenAI:
    return AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version="2024-12-01-preview",
    )


def _deployment() -> str:
    return os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")


def _profile_value(profile: dict, profile_key: str) -> str:
    academic = profile.get("academic", {})
    mapping = {
        "name": profile.get("name", ""),
        "email": profile.get("email", ""),
        "phone": profile.get("phone", ""),
        "student_id": profile.get("student_id", ""),
        "faculty": academic.get("faculty", ""),
        "programme": academic.get("programme", ""),
        "gpa": str(academic.get("gpa", "")),
        "year_of_study": str(academic.get("year_of_study", "")),
        "level": academic.get("level", ""),
        "country_of_origin": academic.get("nationality", {}).get("country_of_origin", ""),
        "local_status": academic.get("nationality", {}).get("local_status", ""),
    }
    return str(mapping.get(profile_key, "") or "")


def _validate_schema(schema: dict, form_payload: dict) -> dict:
    tables = form_payload.get("tables") or []
    table_count = len(tables)

    def valid_table_index(value) -> bool:
        try:
            index = int(value)
        except (TypeError, ValueError):
            return False
        return 0 <= index < table_count

    cleaned = {
        "simple_fields": [],
        "repeating_lists": [],
        "long_text": [],
        "booleans": [],
    }

    for field in schema.get("simple_fields", []) or []:
        if field.get("anchor_label") and valid_table_index(field.get("table_index", 0)):
            cleaned["simple_fields"].append(field)

    for field in schema.get("repeating_lists", []) or []:
        headers = field.get("column_headers") or []
        if field.get("key") and headers and valid_table_index(field.get("table_index", 0)):
            cleaned["repeating_lists"].append(field)

    for field in schema.get("long_text", []) or []:
        if field.get("anchor_label") and field.get("key"):
            cleaned["long_text"].append(field)

    for field in schema.get("booleans", []) or []:
        if field.get("anchor_label") and valid_table_index(field.get("table_index", 0)):
            cleaned["booleans"].append(field)

    return cleaned


def analyze_form_schema(table_json: str) -> dict:
    form_payload = json.loads(table_json)
    prompt = _load_prompt("form_schema_analysis.txt").format(form_json=table_json)
    client = _get_openai_client()
    response = client.chat.completions.create(
        model=_deployment(),
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=4000,
        temperature=0.1,
    )
    parsed = parse_llm_json(response.choices[0].message.content or "{}")
    if not isinstance(parsed, dict):
        raise ValueError("Form schema analysis did not return a JSON object")
    return _validate_schema(parsed, form_payload)


def build_filled_data(schema: dict, profile: dict) -> dict:
    simple_values = {}
    for field in schema.get("simple_fields", []):
        key = field.get("key")
        if not key:
            continue
        profile_key = field.get("profile_key") or key
        simple_values[key] = _profile_value(profile, profile_key)

    repeating_values = {}
    structured_lists = profile.get("structured_lists") or {}
    for field in schema.get("repeating_lists", []):
        key = field.get("key")
        if not key:
            continue
        repeating_values[key] = list(structured_lists.get(key) or [])

    long_text_values = {}
    for field in schema.get("long_text", []):
        key = field.get("key")
        if key:
            long_text_values[key] = ""

    boolean_values = {}
    for field in schema.get("booleans", []):
        key = field.get("key")
        if not key:
            continue
        anchor = normalize_text(field.get("anchor_label", ""))
        if "local" in anchor:
            boolean_values[key] = profile.get("academic", {}).get("nationality", {}).get("local_status") == "local"
        else:
            boolean_values[key] = None

    return {
        "simple_fields": simple_values,
        "repeating_lists": repeating_values,
        "long_text": long_text_values,
        "booleans": boolean_values,
    }


def detect_gaps(schema: dict, filled_data: dict, profile: dict) -> list[dict]:
    gaps = []

    for field in schema.get("repeating_lists", []):
        key = field.get("key")
        items = (filled_data.get("repeating_lists") or {}).get(key) or []
        if items:
            continue
        gaps.append({
            "type": "repeating_list",
            "key": key,
            "label": field.get("label") or key.replace("_", " ").title(),
            "schema": field,
            "prompt": (
                f"I see this form requires up to {field.get('max_rows', 'several')} "
                f"{field.get('label') or key.replace('_', ' ')} entries, but your profile "
                "doesn't have this yet. Let's add them!\n\n"
                "Please tell me about your first experience: organization, role, dates, and hours."
            ),
        })

    for field in schema.get("long_text", []):
        key = field.get("key")
        current = (filled_data.get("long_text") or {}).get(key) or ""
        if current.strip():
            continue
        gaps.append({
            "type": "long_text",
            "key": key,
            "label": field.get("anchor_label") or key.replace("_", " ").title(),
            "schema": field,
        })

    if not gaps and not profile.get("activities"):
        logger.info("No structured list gaps detected")

    return gaps


def parse_list_entry(user_text: str, list_schema: dict) -> dict:
    prompt = _load_prompt("form_list_entry_parse.txt").format(
        list_schema=json.dumps(list_schema, ensure_ascii=False, indent=2),
        user_text=user_text,
    )
    client = _get_openai_client()
    response = client.chat.completions.create(
        model=_deployment(),
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=800,
        temperature=0.1,
    )
    parsed = parse_llm_json(response.choices[0].message.content or "{}")
    if not isinstance(parsed, dict):
        return {}
    return {
        "organization": str(parsed.get("organization") or "").strip(),
        "role": str(parsed.get("role") or "").strip(),
        "dates": str(parsed.get("dates") or "").strip(),
        "hours": str(parsed.get("hours") or "").strip(),
        "description": str(parsed.get("description") or "").strip(),
    }


def draft_long_text(field_schema: dict, profile: dict, collected_lists: dict) -> str:
    target_words = int(field_schema.get("target_words") or 800)
    prompt = _load_prompt("form_long_text_draft.txt").format(
        target_words=target_words,
        field_schema=json.dumps(field_schema, ensure_ascii=False, indent=2),
        profile_json=json.dumps(profile, ensure_ascii=False, indent=2),
        collected_lists_json=json.dumps(collected_lists, ensure_ascii=False, indent=2),
    )
    client = _get_openai_client()
    response = client.chat.completions.create(
        model=_deployment(),
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=2500,
        temperature=0.35,
    )
    parsed = parse_llm_json(response.choices[0].message.content or "{}")
    if isinstance(parsed, dict):
        return str(parsed.get("text") or "").strip()
    return ""


def merge_filled_data(base: dict, pending_lists: dict, long_text_drafts: dict) -> dict:
    merged = {
        "simple_fields": dict(base.get("simple_fields") or {}),
        "repeating_lists": dict(base.get("repeating_lists") or {}),
        "long_text": dict(base.get("long_text") or {}),
        "booleans": dict(base.get("booleans") or {}),
    }
    for key, items in (pending_lists or {}).items():
        merged["repeating_lists"][key] = list(items)
    for key, text in (long_text_drafts or {}).items():
        merged["long_text"][key] = text
    return merged
