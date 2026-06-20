"""
agent/handler.py
Copilot Chat entry point for the HKU Campus Agent.
"""
import os
import json
import logging
import re
from copy import deepcopy
from datetime import datetime, timezone
from dotenv import load_dotenv
from openai import AzureOpenAI

load_dotenv()
logger = logging.getLogger(__name__)

openai_client = AzureOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version="2024-12-01-preview"
)

# Import agent modules using absolute paths from project root
from agent.profile  import get_profile, save_profile, build_profile_from_form, update_profile_fields, extract_cv_text
from agent.matching import run_matching
from agent.drafter  import extract_application_questions, generate_draft_answers
from agent.question_extractor import extract_questions_from_file, extract_text_from_application_file
from agent.form_filler import fill_application_form
from agent.application.docx_parser import extract_form_schema as extract_docx_schema
from agent.application.pdf_parser import extract_form_schema as extract_pdf_schema
from agent.application.form_ai import (
    analyze_form_schema,
    build_filled_data,
    detect_gaps,
    draft_long_text,
    merge_filled_data,
    parse_list_entry,
)
from agent.application.docx_filler import fill_docx_form
from agent.application.pdf_filler import fill_pdf_form
from agent.application.state import (
    clear_application_state,
    get_application_state,
    init_application_state,
    update_application_state,
)
from agent.file_hosting import upload_to_public_host
from agent.digest   import assemble_digest, format_digest_message

# Import event pipeline
from agent.events.event_extractor  import extract_events_for_student
from agent.conflict_checker        import run_conflict_checks_batch

# Import email pipeline
from agent.email_pipeline import run_inbox_pipeline

APPLICATION_FORM_PDF = "/tmp/application_form.pdf"
APPLICATION_FORM_DOCX = "/tmp/application_form.docx"

DEMO_SCHOLARSHIP_472 = {
    "id": "ss_472",
    "scholarship_id": "ss_472",
    "name": "D. H. Chen Foundation Scholarship",
    "application_url": "https://scholar.aas.hku.hk/?action=showonesscheme&ss_id=472",
    "source_url": "https://scholar.aas.hku.hk/?action=showonesscheme&ss_id=472",
}

# ---------------------------------------------------------------------------
# Card loader
# ---------------------------------------------------------------------------
def _load_card(card_name: str) -> dict:
    """Load an Adaptive Card JSON from the copilot/cards directory."""
    card_path = os.path.join(
        os.path.dirname(__file__), "..", "copilot", "cards", f"{card_name}.json"
    )
    with open(card_path) as f:
        return json.load(f)

def _choice_set(input_id: str, label: str, choices: list[dict]) -> dict:
    """Build a compact ChoiceSet for dynamically generated onboarding rows."""
    return {
        "type": "Input.ChoiceSet",
        "id": input_id,
        "label": label,
        "style": "compact",
        "choices": choices
    }

def _timetable_row(row_num: int) -> dict:
    """Build one timetable row for the onboarding card."""
    day_choices = [
        {"title": "Mon", "value": "Monday"},
        {"title": "Tue", "value": "Tuesday"},
        {"title": "Wed", "value": "Wednesday"},
        {"title": "Thu", "value": "Thursday"},
        {"title": "Fri", "value": "Friday"}
    ]
    start_choices = [
        {"title": "09:00", "value": "09:00"},
        {"title": "10:00", "value": "10:00"},
        {"title": "11:00", "value": "11:00"},
        {"title": "12:00", "value": "12:00"},
        {"title": "13:00", "value": "13:00"},
        {"title": "14:00", "value": "14:00"},
        {"title": "15:00", "value": "15:00"},
        {"title": "16:00", "value": "16:00"},
        {"title": "17:00", "value": "17:00"},
        {"title": "18:00", "value": "18:00"}
    ]
    end_choices = start_choices[1:] + [{"title": "19:00", "value": "19:00"}]

    return {
        "type": "ColumnSet",
        "columns": [
            {
                "type": "Column",
                "width": "stretch",
                "items": [
                    {
                        "type": "Input.Text",
                        "id": f"class{row_num}_code",
                        "label": "Course Code",
                        "placeholder": "e.g. COMP3230"
                    }
                ]
            },
            {
                "type": "Column",
                "width": "auto",
                "items": [_choice_set(f"class{row_num}_day", "Day", day_choices)]
            },
            {
                "type": "Column",
                "width": "auto",
                "items": [_choice_set(f"class{row_num}_start", "Start", start_choices)]
            },
            {
                "type": "Column",
                "width": "auto",
                "items": [_choice_set(f"class{row_num}_end", "End", end_choices)]
            }
        ]
    }

def _remove_default_value(items: list, input_id: str) -> None:
    """Remove default values from optional fields in a card tree."""
    for item in items:
        if item.get("id") == input_id:
            item.pop("value", None)
        if item.get("items"):
            _remove_default_value(item["items"], input_id)
        for column in item.get("columns", []):
            _remove_default_value(column.get("items", []), input_id)

def _build_onboarding_card(num_rows: int = 1) -> dict:
    """Build the onboarding card."""
    return deepcopy(_load_card("onboarding_card"))

def _walk_card_items(items: list):
    """Yield every Adaptive Card element nested under body/items/columns."""
    for item in items:
        yield item
        if item.get("items"):
            yield from _walk_card_items(item["items"])
        for column in item.get("columns", []):
            yield from _walk_card_items(column.get("items", []))

def _set_card_input_value(card: dict, input_id: str, value) -> None:
    if value is None:
        return
    for item in _walk_card_items(card.get("body", [])):
        if item.get("id") == input_id:
            item["value"] = str(value)

def _format_prefill_list(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()

def _build_prefilled_onboarding_card(profile: dict) -> dict:
    """Build an edit-profile card using the onboarding schema and saved values."""
    card = _build_onboarding_card()
    body = card.get("body", [])
    if body and body[0].get("type") == "TextBlock":
        body[0]["text"] = "Update Your Profile"
    if len(body) > 1 and body[1].get("type") == "TextBlock":
        body[1]["text"] = "Review and update your saved preferences below."

    academic = profile.get("academic", {})
    financial = profile.get("financial", {})
    nationality = academic.get("nationality", {})
    preferences = profile.get("preferences", {})
    modules_enabled = set(preferences.get("modules_enabled", ["scholarships", "events", "inbox"]))

    input_values = {
        "name": profile.get("name"),
        "faculty": academic.get("faculty"),
        "programme": academic.get("programme"),
        "year_of_study": academic.get("year_of_study"),
        "local_status": nationality.get("local_status"),
        "gpa": academic.get("gpa"),
        "financial_need_opt_in": str(bool(financial.get("financial_need_opt_in"))).lower(),
        "interests": _format_prefill_list(profile.get("interests", [])),
        "activities": _format_prefill_list(profile.get("activities", [])),
        "module_scholarships": "true" if "scholarships" in modules_enabled else "false",
        "module_events": "true" if "events" in modules_enabled else "false",
        "module_inbox": "true" if "inbox" in modules_enabled else "false",
        "notification_preference": preferences.get("notification_preference", "daily_morning")
    }

    for input_id, value in input_values.items():
        _set_card_input_value(card, input_id, value)

    for index, slot in enumerate(profile.get("timetable", {}).get("blocked_slots", [])[:3], start=1):
        _set_card_input_value(card, f"class{index}_code", slot.get("label"))
        _set_card_input_value(card, f"class{index}_day", slot.get("day"))
        _set_card_input_value(card, f"class{index}_start", slot.get("start"))
        _set_card_input_value(card, f"class{index}_end", slot.get("end"))

    for action in card.get("actions", []):
        if action.get("type") == "Action.Submit":
            action["title"] = "Save profile changes"

    return card

# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------
def _text_response(text: str) -> dict:
    return {"type": "message", "text": text}

def _card_response(text: str, card: dict) -> dict:
    return {
        "type": "message",
        "text": text,
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card
            }
        ]
    }

def _file_download_response(filename: str, file_bytes: bytes, content_type: str, text: str = "") -> dict:
    return {
        "type": "message",
        "text": text,
        "file_download": {
            "filename": filename,
            "content_type": content_type,
            "bytes": file_bytes,
        },
    }


def _filled_form_download_card(public_url: str) -> dict:
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.3",
        "body": [
            {
                "type": "TextBlock",
                "text": "Your application form is filled and ready!",
                "weight": "Bolder",
                "size": "Large",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": "Click the button below to download your completed form.",
                "wrap": True,
            },
        ],
        "actions": [
            {
                "type": "Action.OpenUrl",
                "title": "Download Filled Form",
                "url": public_url,
            }
        ],
    }


def _public_download_response(public_url: str) -> list:
    return [
        _text_response("Approved! Your filled application form is ready to download."),
        _card_response(
            "Download your completed application form:",
            _filled_form_download_card(public_url),
        ),
    ]


def _help_card_response() -> dict:
    try:
        card = _load_card("help_card")
        return _card_response("Here is how I can help you:", card)
    except Exception:
        return _text_response("Type 'digest' for updates, 'scholarships' to browse, or 'help' for commands.")

def _get_profile_field(profile: dict, field: str):
    academic = profile.get("academic", {})
    financial = profile.get("financial", {})
    nationality = academic.get("nationality", {})

    if field == "name":
        return profile.get("name")
    if field in ("faculty", "programme"):
        return academic.get(field)
    if field == "year_of_study":
        return academic.get("year_of_study")
    if field == "gpa":
        return academic.get("gpa")
    if field == "local_status":
        return nationality.get("local_status")
    if field == "financial_need_opt_in":
        return financial.get("financial_need_opt_in")
    if field == "notification_preference":
        return profile.get("preferences", {}).get("notification_preference")
    return profile.get(field)

def _set_profile_field(profile: dict, field: str, value) -> None:
    if field == "name":
        profile["name"] = value
    elif field in ("faculty", "programme"):
        profile.setdefault("academic", {})[field] = value
    elif field == "year_of_study":
        profile.setdefault("academic", {})["year_of_study"] = value
    elif field == "gpa":
        try:
            profile.setdefault("academic", {})["gpa"] = float(value)
        except (TypeError, ValueError):
            profile.setdefault("academic", {})["gpa"] = value
    elif field == "local_status":
        profile.setdefault("academic", {}).setdefault("nationality", {})["local_status"] = value
    elif field == "financial_need_opt_in":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "yes", "y", "1")
        profile.setdefault("financial", {})["financial_need_opt_in"] = bool(value)
    elif field == "notification_preference":
        profile.setdefault("preferences", {})["notification_preference"] = value
        profile.setdefault("preferences", {})["digest_frequency"] = _digest_frequency_from_preference(value)
    elif field == "interests":
        current_interests = profile.get("interests", [])
        if not isinstance(current_interests, list):
            current_interests = []
        if isinstance(value, list):
            new_values = value
        else:
            new_values = [value]
        seen = {str(item).strip().lower() for item in current_interests if str(item).strip()}
        for item in new_values:
            cleaned = str(item).strip()
            key = cleaned.lower()
            if cleaned and key not in seen:
                current_interests.append(cleaned)
                seen.add(key)
        profile["interests"] = current_interests
    elif field == "activities":
        if isinstance(value, list):
            profile["activities"] = [str(item).strip() for item in value if str(item).strip()]
        else:
            profile["activities"] = [str(value).strip()] if str(value).strip() else []
    else:
        profile[field] = value

def _restore_profile_field(profile: dict, field: str, value) -> None:
    if field == "interests":
        profile["interests"] = value if isinstance(value, list) else []
    else:
        _set_profile_field(profile, field, value)

FIELD_LABELS = {
    "name": "Name",
    "faculty": "Faculty",
    "programme": "Programme",
    "year_of_study": "Year of Study",
    "local_status": "Student Status",
    "interests": "Interests",
    "activities": "Activities",
    "gpa": "CGPA",
    "financial_need_opt_in": "Financial Need",
    "notification_preference": "Notification Preference",
    "module_scholarships": "Scholarships Module",
    "module_events": "Events Module",
    "module_inbox": "Inbox Module"
}

SENSITIVE_PROFILE_FIELDS = {"faculty", "programme", "year_of_study", "local_status"}

def _field_label(field: str) -> str:
    return FIELD_LABELS.get(field, str(field).replace("_", " ").title())

def _digest_frequency_from_preference(preference: str) -> dict:
    return {
        "daily_morning": {
            "email": "daily",
            "scholarships": "weekly",
            "events": "daily"
        },
        "weekly_summary": {
            "email": "weekly",
            "scholarships": "weekly",
            "events": "weekly"
        },
        "urgent_only": {
            "email": "urgent",
            "scholarships": "weekly",
            "events": "urgent"
        }
    }.get(preference, {
        "email": "daily",
        "scholarships": "weekly",
        "events": "daily"
    })

def _profile_card_response(student_id: str) -> dict:
    profile = get_profile(student_id)
    if not profile:
        return _card_response("Please complete onboarding first.", _build_onboarding_card())

    academic = profile.get("academic", {})
    nationality = academic.get("nationality", {})
    financial = profile.get("financial", {})
    preferences = profile.get("preferences", {})
    facts = [
        {"title": "Name", "value": str(profile.get("name", "Student"))},
        {"title": "Faculty", "value": str(academic.get("faculty", "Not set"))},
        {"title": "Programme", "value": str(academic.get("programme", "Not set"))},
        {"title": "Year of Study", "value": str(academic.get("year_of_study", "Not set"))},
        {"title": "Student Status", "value": str(nationality.get("local_status", "Not set"))},
        {"title": "CGPA", "value": str(academic.get("gpa", "Not set"))},
        {"title": "Financial Need", "value": "Yes" if financial.get("financial_need_opt_in") else "No"},
        {"title": "Interests", "value": ", ".join(profile.get("interests", [])) or "Not set"},
        {"title": "Activities", "value": ", ".join(profile.get("activities", [])) or "Not set"},
        {"title": "Enabled Modules", "value": ", ".join(preferences.get("modules_enabled", [])) or "None"},
        {"title": "Notification Frequency", "value": json.dumps(preferences.get("digest_frequency", {}))}
    ]
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": "Your Profile",
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True
            },
            {
                "type": "FactSet",
                "facts": facts
            }
        ],
        "actions": [
            {
                "type": "Action.Submit",
                "title": "Edit Profile",
                "data": {
                    "action": "edit_profile"
                }
            }
        ]
    }
    return _card_response("Here are your current profile settings:", card)

def _normalize_update_field(field: str) -> str | None:
    if not field:
        return None
    field_key = str(field).strip().lower().replace(" ", "_")
    aliases = {
        "major": "programme",
        "program": "programme",
        "programme": "programme",
        "year": "year_of_study",
        "year_of_study": "year_of_study",
        "status": "local_status",
        "student_status": "local_status",
        "local_status": "local_status",
        "gpa": "gpa",
        "cgpa": "gpa",
        "faculty": "faculty",
        "interests": "interests",
        "interest": "interests",
        "activities": "activities",
        "activity": "activities",
        "financial_need": "financial_need_opt_in",
        "financial_need_opt_in": "financial_need_opt_in",
        "financial_need_info": "financial_need_opt_in",
        "name": "name",
        "display_name": "name",
        "notification": "notification_preference",
        "notification_preference": "notification_preference",
        "digest": "notification_preference",
        "digest_frequency": "notification_preference",
        "module_scholarships": "module_scholarships",
        "module_events": "module_events",
        "module_inbox": "module_inbox",
        "scholarships": "module_scholarships",
        "events": "module_events",
        "inbox": "module_inbox"
    }
    return aliases.get(field_key)

def _normalize_update_value(field: str, value):
    if value is None:
        return value
    if field == "faculty":
        key = str(value).strip().lower()
        faculty_map = {
            "business": "Business and Economics",
            "business and economics": "Business and Economics",
            "fbe": "Business and Economics",
            "arts": "Arts",
            "art": "Arts",
            "eng": "Engineering",
            "engineering": "Engineering",
            "science": "Science",
            "sci": "Science",
            "law": "Law",
            "medicine": "Medicine",
            "med": "Medicine",
            "dentistry": "Dentistry",
            "education": "Education",
            "architecture": "Architecture",
            "social science": "Social Sciences",
            "social sciences": "Social Sciences",
            "computing": "School of Computing and Data Science",
            "computer science": "School of Computing and Data Science",
            "school of computing and data science": "School of Computing and Data Science"
        }
        return faculty_map.get(key, str(value).strip())
    if field == "local_status":
        key = str(value).strip().lower().replace(" ", "").replace("_", "-")
        if key in ("nonlocal", "non-local", "international", "nonhk", "non-hk"):
            return "non-local"
        if key in ("local", "hk", "hongkong", "hong-kong"):
            return "local"
        return str(value).strip()
    if field == "financial_need_opt_in":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "yes", "y", "1", "need", "needed")
    if field in ("module_scholarships", "module_events", "module_inbox"):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "yes", "y", "1", "on", "enable", "enabled", "keep")
    if field == "notification_preference":
        key = str(value).strip().lower().replace(" ", "_")
        if key in ("weekly", "weekly_summary", "week"):
            return "weekly_summary"
        if key in ("urgent", "urgent_only", "only_urgent"):
            return "urgent_only"
        if key in ("daily", "daily_morning", "morning"):
            return "daily_morning"
        return str(value).strip()
    if field == "year_of_study":
        text_value = str(value).strip().lower()
        if "post" in text_value:
            return "postgraduate"
        digits = "".join(ch for ch in text_value if ch.isdigit())
        return int(digits) if digits else value
    if field == "gpa":
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if field in ("interests", "activities"):
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [item.strip() for item in str(value).split(",") if item.strip()]
    return value

def _module_name_from_field(field: str) -> str | None:
    return {
        "module_scholarships": "scholarships",
        "module_events": "events",
        "module_inbox": "inbox"
    }.get(field)

def _apply_module_updates(profile: dict, module_updates: dict) -> bool:
    preferences = profile.setdefault("preferences", {})
    current_modules = preferences.get("modules_enabled", ["scholarships", "events", "inbox"])
    if not isinstance(current_modules, list):
        current_modules = ["scholarships", "events", "inbox"]
    enabled = {str(module) for module in current_modules}

    for field, value in module_updates.items():
        module_name = _module_name_from_field(field)
        if not module_name:
            continue
        if _normalize_update_value(field, value):
            enabled.add(module_name)
        else:
            enabled.discard(module_name)

    if not enabled:
        return False

    ordered = [module for module in ("scholarships", "events", "inbox") if module in enabled]
    preferences["modules_enabled"] = ordered
    return True

def _apply_list_update(profile: dict, field: str, items, operation: str = "add") -> list:
    operation = (operation or "add").lower()
    current = profile.get(field, [])
    if not isinstance(current, list):
        current = []
    values = _normalize_update_value(field, items)
    if not isinstance(values, list):
        values = [values]
    cleaned_values = [str(item).strip() for item in values if str(item).strip()]

    if operation == "remove":
        remove_set = {item.lower() for item in cleaned_values}
        profile[field] = [item for item in current if str(item).strip().lower() not in remove_set]
    elif operation == "set":
        profile[field] = cleaned_values
    else:
        seen = {str(item).strip().lower() for item in current if str(item).strip()}
        for item in cleaned_values:
            key = item.lower()
            if key not in seen:
                current.append(item)
                seen.add(key)
        profile[field] = current
    return profile[field]

def _format_time_value(value) -> str:
    text_value = str(value or "").strip().lower()
    if not text_value:
        return ""
    if ":" in text_value:
        hour, minute = text_value.split(":", 1)
        return f"{int(hour):02d}:{int(minute[:2]):02d}" if hour.isdigit() and minute[:2].isdigit() else text_value
    suffix = "pm" if "pm" in text_value else "am" if "am" in text_value else ""
    digits = "".join(ch for ch in text_value if ch.isdigit())
    if not digits:
        return text_value
    hour = int(digits)
    if suffix == "pm" and hour < 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:00"

def _looks_like_timetable_message(text: str) -> bool:
    return bool(re.search(r"\b[a-z]{3,5}\d{4}\b", text or "", re.IGNORECASE))

PROFILE_UPDATE_SYSTEM_PROMPT = """You are an intelligent university campus agent. Analyze the user's message to determine their intent and extract relevant information.

You can handle these main intents:
1. "update_profile": Changing personal details. Valid fields: name, faculty, programme, year_of_study, local_status, gpa, financial_need, notification_preference, module_scholarships (bool), module_events (bool), module_inbox (bool).
2. "update_interests": Adding or removing items from the user's 'interests' list.
3. "update_activities": Adding or removing items from the user's 'activities' list.
4. "add_timetable": Adding a class to their schedule (requires: course_code, day, start_time, end_time).

Return ONLY a raw JSON object with this exact structure:
{
  "intent": "update_profile" | "update_interests" | "update_activities" | "add_timetable" | "unknown",
  "extracted_data": { ...key-value pairs of what the user provided... },
  "missing_fields": [ "list of fields still needed to complete the action" ],
  "agent_response": "A natural, friendly response to the user. If there are missing_fields, ask them for the missing info. If the action is complete, confirm it."
}

Examples of expected behavior:
- If the user says "add comp1111", intent is "add_timetable", extracted_data has course_code="COMP1111", missing_fields are ["day", "start_time", "end_time"]. agent_response should ask for the missing times.
- If the user says "comp1111 at 10am", extracted_data has course_code="COMP1111", start_time="10:00", missing_fields are ["day", "end_time"].
- If the user says "change faculty to business", map "business" to "Business and Economics". intent is "update_profile", extracted_data has faculty="Business and Economics", missing_fields is empty.
- If the user says "add AI and robotics to my interests", intent is "update_interests", extracted_data has action="add", items=["AI", "robotics"].
- If the user says "remove robotics from interests", intent is "update_interests", extracted_data has action="remove", items=["robotics"].
- If the user says "turn off events but keep scholarships", intent is "update_profile", extracted_data has module_events=false, module_scholarships=true.
- If the user says "change my name to Alex", intent is "update_profile", extracted_data has name="Alex".
- If the user says "make my digest weekly", intent is "update_profile", extracted_data has notification_preference="weekly".
- If you cannot understand the intent, return intent="unknown".
- Capitalize field names in your agent_response (e.g. use "Faculty" instead of "faculty").
"""

def _parse_profile_update(text: str) -> dict:
    response = openai_client.chat.completions.create(
        model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o"),
        messages=[
            {"role": "system", "content": PROFILE_UPDATE_SYSTEM_PROMPT},
            {"role": "user", "content": text}
        ],
        response_format={"type": "json_object"},
        temperature=0
    )
    return json.loads(response.choices[0].message.content)

def _merge_timetable_data(existing: dict, new_data: dict) -> dict:
    merged = dict(existing or {})
    for key, value in (new_data or {}).items():
        if value not in (None, ""):
            merged[key] = value
    return merged

def _extract_timetable_fields_locally(text: str) -> dict:
    data = {}
    raw_text = text or ""
    lowered = raw_text.lower()
    day_aliases = {
        "mon": "Monday",
        "monday": "Monday",
        "tue": "Tuesday",
        "tues": "Tuesday",
        "tuesday": "Tuesday",
        "wed": "Wednesday",
        "wednesday": "Wednesday",
        "thu": "Thursday",
        "thur": "Thursday",
        "thurs": "Thursday",
        "thursday": "Thursday",
        "fri": "Friday",
        "friday": "Friday"
    }
    for alias, day in day_aliases.items():
        if re.search(rf"\b{alias}\b", lowered):
            data["day"] = day
            break

    course_match = re.search(r"\b([a-z]{3,5}\d{4})\b", raw_text, re.IGNORECASE)
    if course_match:
        data["course_code"] = course_match.group(1).upper()

    time_matches = re.findall(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b", lowered)
    if time_matches:
        data["start_time"] = _format_time_value(time_matches[0])
    if len(time_matches) > 1:
        data["end_time"] = _format_time_value(time_matches[1])
    return data

def _missing_timetable_fields(data: dict) -> list[str]:
    required = {
        "course_code": data.get("course_code"),
        "day": data.get("day"),
        "start_time": _format_time_value(data.get("start_time")),
        "end_time": _format_time_value(data.get("end_time"))
    }
    return [field for field, value in required.items() if not str(value or "").strip()]

def _timetable_missing_prompt(data: dict, missing_fields: list[str]) -> str:
    course = str(data.get("course_code") or "that class").upper()
    labels = {
        "course_code": "course code",
        "day": "day",
        "start_time": "start time",
        "end_time": "end time"
    }
    missing_text = ", ".join(labels.get(field, field) for field in missing_fields)
    return f"I can add {course} to your timetable. Please send the missing {missing_text}."

def _append_timetable_slot(profile: dict, data: dict) -> str:
    course_code = str(data.get("course_code", "") or "").strip().upper()
    day = str(data.get("day", "") or "").strip()
    start_time = _format_time_value(data.get("start_time"))
    end_time = _format_time_value(data.get("end_time"))
    timetable = profile.setdefault("timetable", {})
    blocked_slots = timetable.setdefault("blocked_slots", [])
    blocked_slots.append({
        "day": day,
        "start": start_time,
        "end": end_time,
        "label": course_code
    })
    return f"Added {course_code} on {day} from {start_time} to {end_time} to your timetable."

def _save_pending_timetable(profile: dict, data: dict, response_text: str | None = None) -> list:
    missing_fields = _missing_timetable_fields(data)
    profile["pending_action"] = "complete_timetable"
    profile["pending_data"] = data
    save_profile(profile)
    return [_text_response(response_text or _timetable_missing_prompt(data, missing_fields))]

def _clear_pending_state(profile: dict) -> None:
    profile.pop("pending_action", None)
    profile.pop("pending_data", None)

def _handle_pending_timetable(profile: dict, text: str) -> list:
    try:
        parsed = {}
        try:
            parsed = _parse_profile_update(text)
        except Exception as e:
            logger.warning(f"Pending timetable parser fallback: {e}")
        new_data = _merge_timetable_data(
            parsed.get("extracted_data", {}) or {},
            _extract_timetable_fields_locally(text)
        )
        data = _merge_timetable_data(profile.get("pending_data", {}), new_data)
        missing_fields = _missing_timetable_fields(data)
        if missing_fields:
            return _save_pending_timetable(
                profile,
                data,
                parsed.get("agent_response") or _timetable_missing_prompt(data, missing_fields)
            )

        message = _append_timetable_slot(profile, data)
        _clear_pending_state(profile)
        save_profile(profile)
        return [_text_response(message)]
    except Exception as e:
        logger.error(f"Pending timetable update error: {e}")
        return [_text_response("I had trouble completing that timetable update. Please send the class details again.")]

def _apply_profile_update(profile: dict, field: str, value, operation: str = "set") -> None:
    operation = (operation or "set").lower()
    if field == "interests":
        current = profile.get("interests", [])
        if not isinstance(current, list):
            current = []
        values = value if isinstance(value, list) else [value]
        cleaned_values = [str(item).strip() for item in values if str(item).strip()]
        if operation == "remove":
            remove_set = {item.lower() for item in cleaned_values}
            profile["interests"] = [item for item in current if str(item).strip().lower() not in remove_set]
        elif operation == "set":
            profile["interests"] = cleaned_values
        else:
            seen = {str(item).strip().lower() for item in current if str(item).strip()}
            for item in cleaned_values:
                key = item.lower()
                if key not in seen:
                    current.append(item)
                    seen.add(key)
            profile["interests"] = current
        return
    _set_profile_field(profile, field, value)

def _profile_update_confirmation_card(field: str, old_value, new_value, operation: str = "set") -> dict:
    label = _field_label(field)
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": f"You're changing {label} from '{old_value}' to '{new_value}'.",
                "weight": "Bolder",
                "wrap": True
            },
            {
                "type": "TextBlock",
                "text": "This will affect your scholarship matches.",
                "wrap": True,
                "color": "Warning"
            }
        ],
        "actions": [
            {
                "type": "Action.Submit",
                "title": "✅ Confirm",
                "style": "positive",
                "data": {
                    "action": "confirm_profile_update",
                    "field": field,
                    "new_value": new_value,
                    "old_value": old_value,
                    "operation": operation
                }
            },
            {
                "type": "Action.Submit",
                "title": "❌ Cancel",
                "data": {
                    "action": "cancel_profile_update"
                }
            }
        ]
    }
    return _card_response("Please confirm this profile change:", card)

def _scholarship_identifier(scholarship: dict) -> str:
    return str(
        scholarship.get("scholarship_id")
        or scholarship.get("id")
        or scholarship.get("source_id")
        or ""
    ).strip()

def _is_internal_scholarship(scholarship: dict) -> bool:
    return _scholarship_identifier(scholarship).startswith("ss_")

def _scholarship_source_label(scholarship: dict) -> str:
    return "HKU Internal" if _is_internal_scholarship(scholarship) else "External Opportunity"

def _scholarship_url(scholarship: dict) -> str:
    scholarship_id = _scholarship_identifier(scholarship)
    if scholarship_id.startswith("ss_"):
        clean_id = scholarship_id[3:]
        return f"https://scholar.aas.hku.hk/?action=showonesscheme&ss_id={clean_id}"
    return (
        scholarship.get("source_url")
        or scholarship.get("application_url")
        or scholarship.get("url")
        or ""
    )

def _strong_scholarships(scholarships: list) -> list:
    strict_matches = []
    for scholarship in scholarships or []:
        if str(scholarship.get("match_strength", "")).lower() != "strong":
            continue
        if scholarship.get("qualifies") is not True:
            continue
        strict_matches.append(scholarship)
    program_order = {"exact": 0, "faculty_only": 1}
    return sorted(
        strict_matches,
        key=lambda item: (
            program_order.get(item.get("program_match", "faculty_only"), 1),
            item.get("deadline_iso") or "9999-12-31",
            item.get("name", "")
        )
    )

def _append_scholarship_cards(responses: list, scholarships: list, tier: str, offset: int = 0, limit: int = 3) -> None:
    strict_scholarships = _strong_scholarships(scholarships)
    for card in _scholarship_cards(strict_scholarships[offset:offset + limit], tier):
        responses.append({
            "type": "message",
            "text": "Scholarship match" if tier == "apply_now" else "Scholarship to prepare",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]
        })

def _append_scholarship_sections(responses: list, scholarship_result: dict, show_empty: bool = True) -> None:
    apply_now = _strong_scholarships(scholarship_result.get("apply_now", []))
    if apply_now:
        responses.append(_text_response("**📋 Apply Now**"))
        _append_scholarship_cards(responses, apply_now, "apply_now", limit=3)
        if len(apply_now) > 3:
            responses.append(_text_response(
                f"Showing 3 of {len(apply_now)} scholarships. Type 'show more scholarships' to see the rest."
            ))

    prepare = _strong_scholarships(scholarship_result.get("prepare", []))
    if prepare:
        responses.append(_text_response("**🗓️ Prepare For**"))
        _append_scholarship_cards(responses, prepare, "prepare", limit=3)
        if len(prepare) > 3:
            responses.append(_text_response(
                f"Showing 3 of {len(prepare)} scholarships. Type 'show more scholarships' to see the rest."
            ))

    if show_empty and not apply_now and not prepare:
        responses.append(_text_response(
            "No scholarships with strong matches found right now. Try updating your profile or check back later."
        ))

def _scholarship_cards(scholarships: list, tier: str) -> list:
    """Build a list of Adaptive Card attachments for scholarship results."""
    cards = []
    for s in _strong_scholarships(scholarships):
        is_open   = s.get("is_open", False)
        strength  = s.get("match_strength", "strong")
        strength_emoji = "🟢" if strength == "strong" else "🟡"
        source_label = _scholarship_source_label(s)

        body = [
            {
                "type": "TextBlock",
                "text": f"{strength_emoji} {s.get('name', 'Scholarship')}",
                "weight": "Bolder",
                "wrap": True
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Source",    "value": source_label},
                    {"title": "Match",     "value": strength.capitalize()},
                    {"title": "Deadline",  "value": s.get("deadline_raw", "See scholarship page")},
                    {"title": "Reason",    "value": s.get("reason", " ")},
                ]
            }
        ]

        if s.get("gap"):
            body.append({
                "type": "TextBlock",
                "text": f"️ Gap: {s['gap']}",
                "wrap": True,
                "color": "Warning",
                "size": "Small"
            })

        if s.get("application_notes"):
            body.append({
                "type": "TextBlock",
                "text": f"💡 {s['application_notes']}",
                "wrap": True,
                "size": "Small",
                "color": "Accent"
            })

        if s.get("calendar_note"):
            body.append({
                "type": "TextBlock",
                "text": f" {s['calendar_note']}",
                "wrap": True,
                "size": "Small",
                "color": "Warning"
            })

        actions = []
        scholarship_id = _scholarship_identifier(s)
        if scholarship_id == "ss_472" or s.get("is_prototype"):
            actions.append({
                "type": "Action.Submit",
                "title": "Start Application",
                "style": "positive",
                "data": {"action": "start_app_472"},
            })
        elif is_open:
            actions.append({
                "type": "Action.Submit",
                "title": "Start Draft",
                "style": "positive",
                "data": {
                    "action": "start_draft",
                    "scholarship_id": scholarship_id,
                    "scholarship_name": s.get("name", "Scholarship")
                }
            })

        scholarship_url = _scholarship_url(s)
        if scholarship_url:
            actions.append({
                "type": "Action.OpenUrl",
                "title": "View Scholarship",
                "url": scholarship_url
            })

        card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.3",
            "body": body,
            "actions": actions
        }
        cards.append(card)
    return cards

def _is_pdf_upload(content_type: str, filename: str) -> bool:
    return (content_type or "").lower() == "application/pdf" or (filename or "").lower().endswith(".pdf")

def _is_docx_upload(content_type: str, filename: str) -> bool:
    content_type = (content_type or "").lower()
    return (
        content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        or (filename or "").lower().endswith(".docx")
    )

def _event_card(event: dict) -> dict:
    """Build an Adaptive Card for a single event."""
    type_emoji = {
        "competition": "🏆", "hackathon": "💻", "scholarship": "",
        "internship": "💼", "workshop": "🛠️", "talk": "🎤",
        "cultural_exchange": "🌏", "volunteering": "🤝",
        "career_fair": "👔", "recruitment": "📢", "research": "🔬"
    }.get(event.get("type", "other"), "")

    body = [
        {
            "type": "TextBlock",
            "text": f"{type_emoji} {event.get('title', 'Event')}",
            "weight": "Bolder",
            "wrap": True
        },
        {
            "type": "FactSet",
            "facts": [
                {"title": "Type",       "value": event.get("type", " ").replace("_", " ").capitalize()},
                {"title": "Organiser",  "value": event.get("organiser", " ")},
                {"title": "Deadline",   "value": event.get("deadline") or "See event page"},
                {"title": "Location",   "value": event.get("location", " ")},
            ]
        },
        {
            "type": "TextBlock",
            "text": event.get("summary", " "),
            "wrap": True,
            "size": "Small"
        }
    ]

    if event.get("calendar_note"):
        body.append({
            "type": "TextBlock",
            "text": f"⚠️ {event['calendar_note']}",
            "wrap": True,
            "color": "Warning",
            "size": "Small"
        })

    actions = []
    if event.get("source_url"):
        actions.append({
            "type": "Action.OpenUrl",
            "title": "View Event",
            "url": event["source_url"]
        })

    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.3",
        "body": body,
        "actions": actions
    }

def _archive_review_card(review_item: dict) -> dict:
    """Build an Adaptive Card asking whether an archived email was correct."""
    email_id = review_item.get("email_id", "")
    subject = review_item.get("subject", "Archived email")
    reason = review_item.get("reason", "Classified as noise")
    body_preview = review_item.get("body_preview", "")

    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.3",
        "body": [
            {
                "type": "TextBlock",
                "text": "🔍 **Archive Review: Did I get this right?**",
                "weight": "Bolder",
                "color": "Accent",
                "wrap": True
            },
            {
                "type": "TextBlock",
                "text": f"I archived this email: **{subject}**",
                "wrap": True
            },
            {
                "type": "TextBlock",
                "text": f"Reason: {reason}",
                "size": "Small",
                "color": "Good",
                "wrap": True
            },
            {
                "type": "Input.ChoiceSet",
                "id": "undo_reason",
                "label": "If you move this to inbox, why is it important?",
                "style": "compact",
                "choices": [
                    {"title": "Relevant to my major", "value": "Relevant to my major"},
                    {"title": "Contains a deadline", "value": "Contains a deadline"},
                    {"title": "Important sender", "value": "Important sender"},
                    {"title": "Just wanted to read it", "value": "Just wanted to read it"}
                ]
            }
        ],
        "actions": [
            {
                "type": "Action.Submit",
                "title": "✅ Keep Archived",
                "data": {
                    "action": "keep_archived",
                    "email_id": email_id
                }
            },
            {
                "type": "Action.Submit",
                "title": "📥 Move to Inbox",
                "style": "destructive",
                "data": {
                    "action": "undo_archive",
                    "email_id": email_id,
                    "subject": subject,
                    "body_preview": body_preview
                }
            }
        ]
    }

# ---------------------------------------------------------------------------
# Intent handlers
# ---------------------------------------------------------------------------
def handle_onboarding_submit(student_id: str, form_data: dict) -> list:
    """Process onboarding form submission, save profile, return first digest."""
    logger.info(f"Onboarding submit for {student_id}")

    required_fields = {
        "name": "Display Name",
        "faculty": "Faculty",
        "programme": "Programme",
        "year_of_study": "Year of Study",
        "local_status": "Student Status",
        "interests": "Interests"
    }
    missing_fields = [
        label
        for field, label in required_fields.items()
        if not str(form_data.get(field, "") or "").strip()
    ]
    modules_enabled = any(
        form_data.get(module, "true") == "true"
        for module in ("module_scholarships", "module_events", "module_inbox")
    )
    if not modules_enabled:
        missing_fields.append("At least one module")
    if missing_fields:
        missing_text = "\n".join(f"• {field}" for field in missing_fields)
        return [_text_response(f"⚠️ Please fill in these required fields:\n{missing_text}")]

    raw_gpa = form_data.get("gpa")
    gpa_missing = raw_gpa is None or str(raw_gpa).strip() == ""
    gpa_for_profile = raw_gpa if not gpa_missing else 0

    raw_financial_need = form_data.get("financial_need_opt_in")
    financial_need_missing = raw_financial_need is None or str(raw_financial_need).strip() == ""
    financial_need_value = (
        str(raw_financial_need).lower() == "true"
        if not financial_need_missing
        else False
    )

    # Build and save profile
    profile = build_profile_from_form({
        "name":                  form_data.get("name") or "Student",
        "email":                 form_data.get("email", f"{student_id}@connect.hku.hk"),
        "faculty":               form_data.get("faculty", " "),
        "programme":             form_data.get("programme", " "),
        "year_of_study":         form_data.get("year_of_study", "1"),
        "gpa":                   gpa_for_profile,
        "level":                 "postgraduate" if form_data.get("year_of_study") == "postgraduate" else "undergraduate",
        "local_status":          form_data.get("local_status", "local"),
        "country_of_origin":     form_data.get("country_of_origin", "Hong Kong"),
        "financial_need_opt_in": financial_need_value,
        "interests":             [i.strip() for i in form_data.get("interests", " ").split(",") if i.strip()],
        "activities":            [form_data.get("activities", " ")],
        "notification_preference": form_data.get("notification_preference", "daily_morning"),
        "module_scholarships":   form_data.get("module_scholarships", "true"),
        "module_events":         form_data.get("module_events", "true"),
        "module_inbox":          form_data.get("module_inbox", "true"),
        "expected_graduation_year": 2028,
    })

    # Parse any submitted timetable rows, up to the demo limit of 3.
    blocked_slots = []
    for i in range(1, 4):
        row_keys = [f"class{i}_code", f"class{i}_day", f"class{i}_start", f"class{i}_end"]
        if not any(key in form_data for key in row_keys):
            continue

        code  = str(form_data.get(f"class{i}_code", "") or "").strip()
        day   = str(form_data.get(f"class{i}_day", "") or "").strip()
        start = str(form_data.get(f"class{i}_start", "") or "").strip()
        end   = str(form_data.get(f"class{i}_end", "") or "").strip()

        if code and day and start and end:
            blocked_slots.append({
                "day": day,
                "start": start,
                "end": end,
                "label": code
            })

    # Inject timetable into profile before saving
    profile["timetable"] = {
        "blocked_slots": blocked_slots,
        "upcoming_deadlines": []
    }

    profile["id"] = student_id
    save_profile(profile)

    responses = [_text_response(
        f"Profile set up! Welcome, {profile.get('name', 'Student')}. I've saved your preferences. "
        "What would you like to do now?\n\n"
        "• Type 'digest' for your daily update\n"
        "• Type 'scholarships' to browse matches\n"
        "• Type 'events' to see upcoming competitions and events\n"
        "• Type 'inbox' to review your archived emails\n"
        "• Type 'help' anytime to see all commands"
    )]
    return responses

def handle_cv_upload(student_id: str, pdf_bytes: bytes, filename: str) -> list:
    """Extract CV text from an uploaded PDF and save it to the student profile."""
    cv_text = extract_cv_text(pdf_bytes, filename)
    profile = get_profile(student_id)
    if not profile:
        return [_text_response("I couldn't find your profile yet. Please complete onboarding before uploading your CV.")]

    profile["cv_text"] = cv_text
    save_profile(profile)
    return [_text_response(
        f"✅ CV uploaded! Extracted {len(cv_text)} characters. "
        "I'll use this to improve your matches."
    )]

def handle_get_digest(student_id: str) -> list:
    """Run full matching pipeline and return digest as Adaptive Cards."""
    profile = get_profile(student_id)
    if not profile:
        card = _build_onboarding_card()
        return [_card_response(
            "I don't have your profile yet. Let's get you set up:",
            card
        )]

    modules = profile.get("preferences", {}).get(
        "modules_enabled",
        ["scholarships", "events", "inbox"]
    )

    # 1. Run email inbox pipeline only when enabled
    inbox_summary = None
    if "inbox" in modules:
        try:
            inbox_summary = run_inbox_pipeline(student_id, profile)
        except Exception as e:
            logger.error(f"Email pipeline error: {e}")
            inbox_summary = None

    # 2. Run scholarship matching
    scholarship_result = {"apply_now": [], "prepare": []}
    if "scholarships" in modules:
        try:
            scholarship_result = run_matching(student_id)
        except Exception as e:
            logger.error(f"Scholarship matching error: {e}")
            scholarship_result = {"apply_now": [], "prepare": []}

    # 3. Run event extraction and conflict checking
    checked_events = []
    if "events" in modules:
        try:
            raw_events = extract_events_for_student(student_id)
            checked_events = run_conflict_checks_batch(raw_events, profile)
        except Exception as e:
            logger.error(f"Event pipeline error: {e}")
            checked_events = []

    # 4. Assemble digest
    digest = assemble_digest(
        student_id=student_id,
        scholarship_result=scholarship_result,
        events=checked_events,
        inbox_summary=inbox_summary  # Pass the real inbox summary here
    )

    responses = [_text_response(format_digest_message(digest))]

    if "scholarships" in modules:
        _append_scholarship_sections(responses, digest["scholarships"])

    # Event cards — urgent
    if digest["events"]["urgent"]:
        responses.append(_text_response("**🏆 Events — Closing Soon:**"))
        for event in digest["events"]["urgent"][:3]:
            card = _event_card(event)
            responses.append({
                "type": "message",
                "text": "Event opportunity",
                "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]
            })

    if inbox_summary and inbox_summary.get("archived_items"):
        review_item = inbox_summary["archived_items"][0]
        responses.append({
            "type": "message",
            "text": "Archive review",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": _archive_review_card(review_item)
                }
            ]
        })

    return responses

def handle_get_scholarships(student_id: str) -> list:
    """Run scholarship matching and render only scholarship sections."""
    profile = get_profile(student_id)
    if not profile:
        return [_card_response("I don't have your profile yet. Let's get you set up:", _build_onboarding_card())]

    modules = profile.get("preferences", {}).get("modules_enabled", ["scholarships", "events", "inbox"])
    if "scholarships" not in modules:
        return [_text_response("Scholarship matching is currently turned off in your profile settings.")]

    try:
        scholarship_result = run_matching(student_id)
    except Exception as e:
        logger.error(f"Scholarship matching error: {e}")
        scholarship_result = {"apply_now": [], "prepare": []}

    responses = [_text_response("Here are your strongest scholarship matches:")]
    _append_scholarship_sections(responses, scholarship_result)
    return responses

def handle_show_more_scholarships(student_id: str) -> list:
    """Render scholarship cards beyond the first digest batch."""
    profile = get_profile(student_id)
    if not profile:
        return [_card_response("I don't have your profile yet. Let's get you set up:", _build_onboarding_card())]

    modules = profile.get("preferences", {}).get("modules_enabled", ["scholarships", "events", "inbox"])
    if "scholarships" not in modules:
        return [_text_response("Scholarship matching is currently turned off in your profile settings.")]

    try:
        scholarship_result = run_matching(student_id)
    except Exception as e:
        logger.error(f"Show more scholarship matching error: {e}")
        return [_text_response("I had trouble loading more scholarships. Please try again later.")]

    responses = []
    apply_now = _strong_scholarships(scholarship_result.get("apply_now", []))
    prepare = _strong_scholarships(scholarship_result.get("prepare", []))

    if len(apply_now) > 3:
        responses.append(_text_response("**📋 More Apply Now Scholarships**"))
        _append_scholarship_cards(responses, apply_now, "apply_now", offset=3, limit=10)

    if len(prepare) > 3:
        responses.append(_text_response("**🗓️ More Scholarships To Prepare For**"))
        _append_scholarship_cards(responses, prepare, "prepare", offset=3, limit=10)

    if not responses:
        return [_text_response("You're already seeing all current scholarship matches.")]
    return responses

def _normalize_question_items(questions: list) -> list[dict]:
    normalized = []
    for idx, question in enumerate(questions or [], start=1):
        if isinstance(question, dict):
            text = str(question.get("text") or question.get("question") or "").strip()
            qid = str(question.get("id") or f"q{idx}").strip()
        else:
            text = str(question).strip()
            qid = f"q{idx}"
        if text:
            normalized.append({"id": qid or f"q{idx}", "text": text})
    return normalized[:10]


def _question_selection_card(scholarship_id: str, questions: list[dict]) -> dict:
    body = [
        {
            "type": "TextBlock",
            "text": "Select Questions to Auto-fill",
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True
        },
        {
            "type": "TextBlock",
            "text": "I found these application questions. Select the ones you want me to draft.",
            "wrap": True
        }
    ]

    for question in questions:
        body.append({
            "type": "Input.Toggle",
            "id": f"fill_{question['id']}",
            "title": question["text"],
            "value": "true",
            "valueOn": "true",
            "valueOff": "false",
            "wrap": True
        })

    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.3",
        "body": body,
        "actions": [
            {
                "type": "Action.Submit",
                "title": "Generate Draft Answers",
                "style": "positive",
                "data": {
                    "action": "generate_draft",
                    "scholarship_id": scholarship_id,
                    "questions": questions
                }
            }
        ]
    }


def _save_last_questions(student_id: str, scholarship_id: str, questions: list[dict]) -> None:
    profile = get_profile(student_id)
    if not profile:
        return
    profile["last_scholarship_id"] = scholarship_id
    profile["last_application_questions"] = questions
    save_profile(profile)


def handle_start_draft(student_id: str, scholarship_id: str, scholarship_name: str = "") -> list:
    """Save draft state and ask the student to upload the form or paste questions."""
    logger.info(f"Starting upload-first draft flow for {scholarship_id} / {student_id}")
    profile = get_profile(student_id)
    if not profile:
        card = _build_onboarding_card()
        return [_card_response("Please complete onboarding before drafting an application.", card)]

    profile["last_scholarship_id"] = scholarship_id
    profile["last_scholarship_name"] = scholarship_name or scholarship_id or "Scholarship"
    profile.pop("last_application_questions", None)
    save_profile(profile)

    return [_text_response(
        f"Great choice: {profile['last_scholarship_name']}. "
        "Please attach the PDF/DOCX application form, or paste the application questions in chat. "
        "I’ll extract the questions first, then let you choose which ones to draft."
    )]


def handle_start_application(student_id: str) -> list:
    """Begin the guided ss_472 application flow and wait for a form upload."""
    profile = get_profile(student_id)
    if not profile:
        return [_card_response("Please complete onboarding before starting an application.", _build_onboarding_card())]

    profile["pending_application"] = "ss_472"
    _clear_application_review_state(profile)
    profile.pop("last_scholarship_id", None)
    profile.pop("last_application_questions", None)
    save_profile(profile)

    return [_text_response(
        "Great choice! Please upload the application form (PDF or DOCX) to this chat."
    )]


def _build_application_answers_text(draft_answers: list, additional_notes: str = "") -> str:
    parts = []
    for item in draft_answers or []:
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if question and answer:
            parts.append(f"Q: {question}\nA: {answer}")
    if additional_notes.strip():
        parts.append(f"Additional notes:\n{additional_notes.strip()}")
    return "\n\n".join(parts)


def _clear_application_review_state(profile: dict) -> None:
    for key in (
        "pending_application_review",
        "application_input_path",
        "application_output_path",
        "application_content_type",
        "application_draft_answers",
        "application_questions",
    ):
        profile.pop(key, None)
    clear_application_state(profile)


def _application_state_active(profile: dict) -> bool:
    state = get_application_state(profile)
    return bool(state.get("step"))


def _list_gaps(gaps: list) -> list:
    return [gap for gap in gaps or [] if gap.get("type") == "repeating_list"]


def _long_text_gaps(gaps: list) -> list:
    return [gap for gap in gaps or [] if gap.get("type") == "long_text"]


def _looks_like_section_advance(text: str) -> bool:
    normalized = (text or "").strip().lower()
    return normalized in {
        "next", "done", "move on", "skip", "no more", "continue",
        "that's all", "thats all", "no more", "move to next section",
    }


def _application_review_card(scholarship_id: str, scholarship_name: str, state: dict) -> dict:
    body = [
        {
            "type": "TextBlock",
            "text": f"Review Application: {scholarship_name}",
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": "Review the collected information below. Approve when you're ready and I'll fill the form.",
            "wrap": True,
        },
    ]

    filled_data = state.get("filled_data") or {}
    pending_lists = state.get("pending_list_data") or {}
    long_text_drafts = state.get("long_text_drafts") or {}

    simple_fields = filled_data.get("simple_fields") or {}
    if simple_fields:
        body.append({
            "type": "TextBlock",
            "text": "Profile fields",
            "weight": "Bolder",
            "wrap": True,
            "spacing": "Medium",
        })
        for key, value in simple_fields.items():
            if value:
                body.append({
                    "type": "TextBlock",
                    "text": f"{key.replace('_', ' ').title()}: {value}",
                    "wrap": True,
                })

    for list_key, items in pending_lists.items():
        if not items:
            continue
        body.append({
            "type": "TextBlock",
            "text": list_key.replace("_", " ").title(),
            "weight": "Bolder",
            "wrap": True,
            "spacing": "Medium",
        })
        for index, item in enumerate(items, start=1):
            summary = ", ".join(
                f"{field}: {value}"
                for field, value in item.items()
                if value
            )
            body.append({
                "type": "TextBlock",
                "text": f"{index}. {summary}",
                "wrap": True,
            })

    for key, text in long_text_drafts.items():
        if not text:
            continue
        body.append({
            "type": "TextBlock",
            "text": key.replace("_", " ").title(),
            "weight": "Bolder",
            "wrap": True,
            "spacing": "Medium",
        })
        preview = text if len(text) <= 1200 else f"{text[:1200]}..."
        body.append({
            "type": "TextBlock",
            "text": preview,
            "wrap": True,
        })

    body.append({
        "type": "Input.Text",
        "id": "additional_notes",
        "placeholder": "Optional: add anything missing from your profile or CV",
        "isMultiline": True,
        "isRequired": False,
    })

    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.3",
        "body": body,
        "actions": [
            {
                "type": "Action.Submit",
                "title": "Approve & Download Form",
                "style": "positive",
                "data": {"action": "approve_application", "scholarship_id": scholarship_id},
            },
            {
                "type": "Action.Submit",
                "title": "Cancel",
                "data": {"action": "cancel_application_review"},
            },
        ],
    }


def _begin_application_review(profile: dict, state: dict) -> list:
    long_text_drafts = {}
    for gap in _long_text_gaps(state.get("gap_queue", [])):
        long_text_drafts[gap["key"]] = draft_long_text(
            gap["schema"],
            profile,
            state.get("pending_list_data") or {},
        )

    state["long_text_drafts"] = long_text_drafts
    state["step"] = "review"
    state["current_gap"] = None
    profile["pending_application_review"] = state.get("scholarship_id", "ss_472")
    profile["application_input_path"] = state.get("input_path")
    profile["application_output_path"] = state.get("output_path")
    profile["application_content_type"] = state.get("content_type")
    update_application_state(profile, **state)
    save_profile(profile)

    scholarship_name = DEMO_SCHOLARSHIP_472.get("name", "Scholarship")
    return [
        _text_response("I've gathered everything needed. Review the summary and approve when you're ready."),
        _card_response(
            "Review your application data before I fill the form.",
            _application_review_card(state.get("scholarship_id", "ss_472"), scholarship_name, state),
        ),
    ]


def _advance_application_gap(profile: dict, state: dict) -> list:
    list_gap_items = _list_gaps(state.get("gap_queue", []))
    current = state.get("current_gap") or {}
    current_index = 0
    if current:
        for index, gap in enumerate(list_gap_items):
            if gap.get("key") == current.get("key"):
                current_index = index + 1
                break

    if current_index < len(list_gap_items):
        next_gap = list_gap_items[current_index]
        update_application_state(
            profile,
            current_gap=next_gap,
            step="collecting_list",
        )
        save_profile(profile)
        return [_text_response(next_gap.get("prompt") or "Please share the next entry.")]

    return _begin_application_review(profile, get_application_state(profile))


def handle_application_collection_message(student_id: str, text: str) -> list | None:
    profile = get_profile(student_id)
    if not profile:
        return None

    state = get_application_state(profile)
    if state.get("step") != "collecting_list":
        return None

    if _looks_like_section_advance(text):
        return _advance_application_gap(profile, state)

    current_gap = state.get("current_gap") or {}
    if current_gap.get("type") != "repeating_list":
        return [_text_response("Say **next** when you're ready to continue.")]

    list_schema = current_gap.get("schema") or {}
    list_key = current_gap.get("key")
    entry = parse_list_entry(text, list_schema)
    if not any(entry.values()):
        return [_text_response(
            "I couldn't parse that entry. Please include organization, role, dates, and hours if possible."
        )]

    pending_list_data = dict(state.get("pending_list_data") or {})
    entries = list(pending_list_data.get(list_key) or [])
    entries.append(entry)
    pending_list_data[list_key] = entries
    update_application_state(profile, pending_list_data=pending_list_data)
    save_profile(profile)

    max_rows = int(list_schema.get("max_rows") or 5)
    if len(entries) >= max_rows:
        return _advance_application_gap(profile, get_application_state(profile))

    label = current_gap.get("label") or list_key.replace("_", " ")
    return [_text_response(
        f"Got it! Do you have another {label} entry to add, or say **next** to move to the next section?"
    )]


def _looks_like_application_approval(text: str) -> bool:
    normalized = (text or "").strip().lower()
    approval_terms = (
        "approve", "approved", "looks good", "look good", "go ahead",
        "confirm", "yes download", "download form", "fill the form",
        "proceed", "that's fine", "thats fine", "all good",
    )
    return any(term in normalized for term in approval_terms)


def handle_application_form_upload(
    student_id: str,
    file_bytes: bytes,
    filename: str,
    content_type: str = ""
) -> list:
    """Analyze an uploaded form, detect gaps, and start conversational collection."""
    profile = get_profile(student_id)
    if not profile:
        return [_text_response("I couldn't find your profile yet. Please complete onboarding first.")]

    if profile.get("pending_application") != "ss_472":
        return [_text_response(
            "Tap **Start Application** on the D. H. Chen Foundation Scholarship card first."
        )]

    if _is_pdf_upload(content_type, filename):
        input_path = APPLICATION_FORM_PDF
        output_path = "/tmp/filled_application_form.pdf"
        stored_content_type = "application/pdf"
    elif _is_docx_upload(content_type, filename):
        input_path = APPLICATION_FORM_DOCX
        output_path = "/tmp/filled_application_form.docx"
        stored_content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    else:
        return [_text_response(
            "Please upload a PDF (`application/pdf`) or DOCX "
            "(`application/vnd.openxmlformats-officedocument.wordprocessingml.document`) file."
        )]

    with open(input_path, "wb") as handle:
        handle.write(file_bytes)

    try:
        if stored_content_type == "application/pdf":
            form_json = extract_pdf_schema(input_path)
        else:
            form_json = extract_docx_schema(input_path)

        schema = analyze_form_schema(form_json)
        filled_data = build_filled_data(schema, profile)
        gaps = detect_gaps(schema, filled_data, profile)
    except Exception as exc:
        logger.error(f"Application form analysis failed: {exc}")
        return [_text_response(
            "I couldn't analyze that form structure. Please upload a table-based DOCX or PDF application form."
        )]

    profile.pop("pending_application", None)
    _clear_application_review_state(profile)

    list_gap_items = _list_gaps(gaps)
    first_gap = list_gap_items[0] if list_gap_items else None
    state = init_application_state(
        profile,
        scholarship_id="ss_472",
        input_path=input_path,
        output_path=output_path,
        content_type=stored_content_type,
        schema=schema,
        filled_data=filled_data,
        step="collecting_list" if first_gap else "review",
    )
    update_application_state(
        profile,
        form_json=form_json,
        gap_queue=gaps,
        current_gap=first_gap,
        pending_list_data={},
        long_text_drafts={},
    )
    profile["last_scholarship_id"] = "ss_472"
    profile["last_scholarship_name"] = DEMO_SCHOLARSHIP_472["name"]
    save_profile(profile)

    if first_gap:
        return [
            _text_response(
                f"I analyzed your form and found {len(schema.get('simple_fields', []))} profile fields "
                f"and {len(schema.get('repeating_lists', []))} repeating sections."
            ),
            _text_response(first_gap.get("prompt") or "Please share the first entry for this section."),
        ]

    return _begin_application_review(profile, get_application_state(profile))


def handle_approve_application(student_id: str, form_data: dict | None = None) -> list:
    """Fill the uploaded form after the student approves the collected application data."""
    form_data = form_data or {}
    profile = get_profile(student_id)
    state = get_application_state(profile) if profile else {}
    if not profile or (
        state.get("step") != "review"
        and profile.get("pending_application_review") != "ss_472"
    ):
        return [_text_response("No application is waiting for approval right now.")]

    if not state:
        state = {
            "input_path": profile.get("application_input_path"),
            "output_path": profile.get("application_output_path"),
            "content_type": profile.get("application_content_type"),
            "schema": {},
            "filled_data": {},
            "pending_list_data": {},
            "long_text_drafts": {},
        }

    input_path = state.get("input_path") or profile.get("application_input_path")
    output_path = state.get("output_path") or profile.get("application_output_path")
    content_type = state.get("content_type") or profile.get("application_content_type")
    if not input_path or not output_path or not os.path.exists(input_path):
        return [_text_response("I couldn't find your uploaded form. Please upload it again.")]

    additional_notes = str(form_data.get("additional_notes") or "").strip()
    merged_data = merge_filled_data(
        state.get("filled_data") or {},
        state.get("pending_list_data") or {},
        state.get("long_text_drafts") or {},
    )
    if additional_notes:
        merged_data.setdefault("long_text", {})
        merged_data["long_text"]["additional_notes"] = additional_notes

    schema = state.get("schema") or {}
    try:
        if content_type == "application/pdf":
            fill_pdf_form(input_path, merged_data, schema, output_path)
        else:
            fill_docx_form(input_path, merged_data, schema, output_path)
    except Exception as exc:
        logger.error(f"Application form fill failed: {exc}")
        return [_text_response(
            "I couldn't fill that form template. Please try uploading the form again."
        )]

    if not os.path.exists(output_path):
        return [_text_response("The filled form could not be generated. Please try again.")]

    public_url = upload_to_public_host(output_path)
    _clear_application_review_state(profile)
    save_profile(profile)

    return _public_download_response(public_url)


def handle_cancel_application_review(student_id: str) -> list:
    profile = get_profile(student_id)
    if profile:
        _clear_application_review_state(profile)
        save_profile(profile)
    return [_text_response("Application review cancelled. Tap **Start Application** whenever you're ready to try again.")]


def handle_form_uploaded(student_id: str, file_bytes: bytes, filename: str) -> list:
    """Extract questions from an uploaded application form."""
    profile = get_profile(student_id)
    if not profile:
        return [_text_response("I couldn't find your profile yet. Please complete onboarding before uploading a form.")]

    scholarship_id = profile.get("last_scholarship_id", "")
    if not scholarship_id:
        return handle_cv_upload(student_id, file_bytes, filename)

    questions = extract_questions_from_file(file_bytes, filename)
    questions = _normalize_question_items(questions)
    if not questions:
        return [_text_response(
            "I couldn't extract application questions from that file. "
            "Please paste the question section from the form in chat instead."
        )]

    _save_last_questions(student_id, scholarship_id, questions)
    return [_card_response(
        "I extracted the application questions. Choose which ones to draft:",
        _question_selection_card(scholarship_id, questions)
    )]


def handle_text_pasted(student_id: str, raw_questions: str) -> list:
    """Extract questions from pasted form text during the active draft flow."""
    profile = get_profile(student_id)
    if not profile or not profile.get("last_scholarship_id"):
        return [_text_response("Tap 'Start Draft' on a scholarship card first, then paste the questions.")]

    scholarship_id = profile["last_scholarship_id"]
    questions = _normalize_question_items(extract_application_questions(raw_questions))
    if not questions:
        return [_text_response(
            "I couldn't find any application questions in that text. "
            "Please paste the essay or short-answer section from the form and try again."
        )]

    _save_last_questions(student_id, scholarship_id, questions)
    return [_card_response(
        "I extracted the application questions. Choose which ones to draft:",
        _question_selection_card(scholarship_id, questions)
    )]


def handle_draft_questions(student_id: str, scholarship_id: str, raw_questions: str) -> list:
    """Compatibility route for older paste-question cards."""
    profile = get_profile(student_id)
    if profile and scholarship_id:
        profile["last_scholarship_id"] = scholarship_id
        save_profile(profile)
    return handle_text_pasted(student_id, raw_questions)


def handle_generate_draft(student_id: str, form_data: dict) -> list:
    """Generate answers for selected extracted questions."""
    scholarship_id = form_data.get("scholarship_id", "")
    questions = _normalize_question_items(form_data.get("questions", []))
    profile = get_profile(student_id)

    if profile and not questions:
        questions = _normalize_question_items(profile.get("last_application_questions", []))
    if profile and not scholarship_id:
        scholarship_id = profile.get("last_scholarship_id", "")

    selected_questions = [
        q["text"]
        for q in questions
        if str(form_data.get(f"fill_{q['id']}", "false")).lower() == "true"
    ]
    if not selected_questions:
        return [_text_response("Please select at least one question to draft.")]

    draft = generate_draft_answers(student_id, scholarship_id, selected_questions)
    if draft.get("error"):
        return [_text_response(f"I couldn't generate the draft yet: {draft['error']}")]

    body = [
        {
            "type": "TextBlock",
            "text": f"Draft Answers: {draft.get('scholarship_name', 'Scholarship')}",
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True
        }
    ]

    for item in draft.get("answers", []):
        body.extend([
            {
                "type": "TextBlock",
                "text": item.get("question", "Question"),
                "weight": "Bolder",
                "wrap": True,
                "spacing": "Medium"
            },
            {
                "type": "TextBlock",
                "text": item.get("answer", ""),
                "wrap": True
            }
        ])

    if draft.get("notes"):
        body.append({
            "type": "TextBlock",
            "text": f"Review note: {draft['notes']}",
            "wrap": True,
            "size": "Small",
            "color": "Accent"
        })

    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.3",
        "body": body
    }

    return [_card_response("Here are your draft answers. Please review and personalize before submitting.", card)]

def handle_profile_update(student_id: str, text: str) -> list:
    """Uses OpenAI to parse natural language profile updates."""
    try:
        parsed = _parse_profile_update(text)
        intent = parsed.get("intent", "unknown")
        extracted_data = parsed.get("extracted_data", {}) or {}
        missing_fields = parsed.get("missing_fields", []) or []
        agent_response = parsed.get("agent_response") or "I need a bit more information to help with that."

        if intent == "unknown":
            return [_text_response(agent_response)]

        profile = get_profile(student_id)
        if not profile:
            return [_text_response("Please complete onboarding first.")]

        if missing_fields:
            if intent == "add_timetable":
                return _save_pending_timetable(profile, extracted_data, agent_response)
            return [_text_response(agent_response)]

        if intent == "update_profile":
            normalized_updates = {}
            module_updates = {}
            for raw_field, raw_value in extracted_data.items():
                field = _normalize_update_field(raw_field)
                if not field:
                    continue
                value = _normalize_update_value(field, raw_value)
                if field.startswith("module_"):
                    module_updates[field] = value
                else:
                    normalized_updates[field] = value

            sensitive_updates = {
                field: value
                for field, value in normalized_updates.items()
                if field in SENSITIVE_PROFILE_FIELDS
            }
            if sensitive_updates:
                field, new_value = next(iter(sensitive_updates.items()))
                return [_profile_update_confirmation_card(
                    field,
                    _get_profile_field(profile, field),
                    new_value,
                    "set"
                )]

            old_values = {field: _get_profile_field(profile, field) for field in normalized_updates}
            for field, value in normalized_updates.items():
                _apply_profile_update(profile, field, value, "set")

            if module_updates and not _apply_module_updates(profile, module_updates):
                return [_text_response("At least one module must stay enabled. I kept your current module settings unchanged.")]

            save_profile(profile)
            if len(normalized_updates) == 1 and not module_updates:
                field, new_value = next(iter(normalized_updates.items()))
                old_value = old_values.get(field)
                card = {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": f"Updated! I changed your **{_field_label(field)}** to **{new_value}**.",
                            "wrap": True
                        }
                    ],
                    "actions": [
                        {
                            "type": "Action.Submit",
                            "title": "Revert Change",
                            "data": {
                                "action": "revert_profile",
                                "field": field,
                                "old_value": old_value
                            }
                        }
                    ]
                }
                return [_card_response(f"Updated {_field_label(field)} to {new_value}.", card)]
            return [_text_response(agent_response)]

        if intent in ("update_interests", "update_activities"):
            field = "interests" if intent == "update_interests" else "activities"
            operation = (extracted_data.get("action") or "add").lower()
            items = extracted_data.get("items", [])
            updated_items = _apply_list_update(profile, field, items, operation)
            save_profile(profile)
            return [_text_response(
                f"Updated {_field_label(field)}: {', '.join(updated_items) if updated_items else 'None'}."
            )]

        if intent == "add_timetable":
            missing_timetable_fields = _missing_timetable_fields(extracted_data)
            if missing_timetable_fields:
                return _save_pending_timetable(profile, extracted_data, agent_response)
            message = _append_timetable_slot(profile, extracted_data)
            save_profile(profile)
            return [_text_response(message)]

        return [_text_response(agent_response)]

    except Exception as e:
        logger.error(f"Profile update error: {e}")
        return [_text_response("I had trouble updating that. Please try again.")]

def handle_semester_refresh(student_id: str, form_data: dict, dismissed: bool = False) -> list:
    """Handle semester refresh form submission."""
    if dismissed:
        return [_text_response("No problem — your profile is unchanged. I'll keep finding opportunities for you.")]

    updates = {}
    if form_data.get("gpa"):
        updates["academic"] = {"gpa": float(form_data["gpa"])}
    if form_data.get("year_of_study"):
        raw_year = str(form_data.get("year_of_study", "1"))
        year_val = raw_year if raw_year == "postgraduate" else int(raw_year)
        updates.setdefault("academic", {})["year_of_study"] = year_val

    if updates:
        update_profile_fields(student_id, updates)
        return [_text_response(
            "Profile updated! Running matching with your new details..."
        )] + handle_get_digest(student_id)
    else:
        return [_text_response("Profile unchanged.")]

# ---------------------------------------------------------------------------
# Main router
# ---------------------------------------------------------------------------
def handle_message(student_id: str, message: dict) -> list:
    """
    Main entry point called by the M365 Agents SDK.
    """
    activity_type = message.get("type", "message")
    text          = (message.get("text") or " ").strip().lower()
    value         = message.get("value") or {}
    action        = str(value.get("action", " "))
    profile       = get_profile(student_id)
    is_edit       = bool(message.get("is_edit"))

    logger.info(f"Message from {student_id}: type={activity_type} action={action} text={text[:50]}")

    if (
        is_edit
        and profile
        and profile.get("onboarding_complete")
        and not profile.get("pending_action")
        and not profile.get("pending_application_review")
        and get_application_state(profile).get("step") != "collecting_list"
    ):
        return [_text_response("For security, I cannot process edits to previous messages. Please send a new message instead.")]

    if profile and profile.get("pending_action") == "complete_timetable" and not action.strip():
        return _handle_pending_timetable(profile, message.get("text") or "")

    if profile and get_application_state(profile).get("step") == "collecting_list" and not action.strip():
        collection_response = handle_application_collection_message(
            student_id,
            message.get("text") or "",
        )
        if collection_response is not None:
            return collection_response

    # ─ Adaptive Card submissions ────────────────────────────────────────────
    if action == "onboarding_submit":
        return handle_onboarding_submit(student_id, value)

    if action == "revert_profile":
        field = value.get("field")
        old_value = value.get("old_value")
        profile = get_profile(student_id)
        if profile and field:
            _restore_profile_field(profile, field, old_value)
            save_profile(profile)
            return [_text_response(f"↩️ Reverted! Your {field} is back to {old_value}.")]
        return [_text_response("Could not revert that change.")]

    if action == "confirm_profile_update":
        field = value.get("field")
        new_value = value.get("new_value")
        operation = value.get("operation", "set")
        profile = get_profile(student_id)
        if profile and field:
            _apply_profile_update(profile, field, new_value, operation)
            save_profile(profile)
            return [_text_response(f"✅ Updated! Your {_field_label(field)} is now {new_value}.")]
        return [_text_response("Could not apply that profile change.")]

    if action == "cancel_profile_update":
        return [_text_response("No problem — I cancelled that profile change.")]

    if action == "edit_profile":
        if profile:
            return [_card_response("Update your profile below:", _build_prefilled_onboarding_card(profile))]
        return [_card_response("Please complete onboarding first.", _build_onboarding_card())]

    if action == "semester_refresh_submit":
        return handle_semester_refresh(student_id, value)

    if action == "semester_refresh_dismiss":
        return handle_semester_refresh(student_id, value, dismissed=True)

    if action == "start_app_472":
        return handle_start_application(student_id)

    if action == "approve_application":
        return handle_approve_application(student_id, value)

    if action == "cancel_application_review":
        return handle_cancel_application_review(student_id)

    if action == "start_draft":
        scholarship_id = value.get("scholarship_id", " ")
        scholarship_name = value.get("scholarship_name", "")
        return handle_start_draft(student_id, scholarship_id, scholarship_name)

    if action == "draft_questions":
        scholarship_id = value.get("scholarship_id", " ")
        raw_questions = value.get("application_questions", "")
        return handle_draft_questions(student_id, scholarship_id, raw_questions)

    if action == "generate_draft":
        return handle_generate_draft(student_id, value)

    if action == "auto_fill_selected_questions":
        # Compatibility with older cards that used this action name.
        return handle_generate_draft(student_id, value)

    if action == "keep_archived":
        return [_text_response("Noted! I'll keep it archived. Thanks for the feedback!")]

    if action == "undo_archive":
        email_id = value.get("email_id", "")
        subject = value.get("subject", "")
        body_preview = value.get("body_preview", "")
        undo_reason = value.get("undo_reason", "not specified")
        if email_id:
            from agent.graph import restore_email
            from agent.learner import extract_learned_interests
            profile = get_profile(student_id)
            user_email = profile.get("email") if profile else None
            success = restore_email(email_id, user_email) if user_email else restore_email(email_id)
            if not success:
                return [_text_response("Could not restore the email.")]

            learned_interests = extract_learned_interests(subject, body_preview)
            new_interests = []

            if profile and learned_interests:
                existing_interests = profile.get("interests", [])
                if not isinstance(existing_interests, list):
                    existing_interests = [str(existing_interests)]

                seen = {str(item).strip().lower() for item in existing_interests if str(item).strip()}
                for interest in learned_interests:
                    cleaned = str(interest).strip()
                    key = cleaned.lower()
                    if cleaned and key not in seen:
                        existing_interests.append(cleaned)
                        new_interests.append(cleaned)
                        seen.add(key)

                if new_interests:
                    profile["interests"] = existing_interests
                    save_profile(profile)

            topics_text = ", ".join(new_interests) if new_interests else "no new topics"
            return [_text_response(
                "✅ Email restored! "
                f"I've noted this is important because it's {undo_reason}. "
                f"I also updated your profile with new interests: {topics_text}. "
                "Thanks for the feedback!"
            )]
        return [_text_response("No email ID provided to restore.")]

    # ── Text messages ────────────────────────────────────────────────────────
    if any(kw in text for kw in ["edit profile", "my settings"]):
        if profile:
            return [_card_response("Update your profile below:", _build_prefilled_onboarding_card(profile))]
        return [_card_response("Please complete onboarding first.", _build_onboarding_card())]

    if any(kw in text for kw in ["show my profile", "what do you know about me", "view profile"]):
        return [_profile_card_response(student_id)]

    if any(kw in text for kw in ["help", "commands", "what can you do"]):
        return [_help_card_response()]

    if "show more scholarships" in text:
        return handle_show_more_scholarships(student_id)

    update_terms = [
        "change my", "update my", "set my", "add to my", "remove from my",
        "change ", "update ", "set ", "modify", "make me", "i want to be",
        "add ", "remove ", "add class", "add course", "add timetable", "add schedule"
    ]
    if any(kw in text for kw in update_terms) or _looks_like_timetable_message(text):
        return handle_profile_update(student_id, text)

    if "upload cv" in text or text == "cv":
        return [_text_response("Please attach your PDF CV to the chat first, then type CV again.")]

    scholarship_commands = {
        "scholarship", "scholarships", "show scholarships",
        "browse scholarships", "show me scholarships", "scholarship matches"
    }
    if profile and profile.get("pending_application_review") and _looks_like_application_approval(text):
        return handle_approve_application(student_id, {})

    if profile and profile.get("pending_application_review") and text:
        return [_text_response(
            "Your draft answers are ready for review. "
            "Use **Approve & Download Form** on the card above, or reply **approve** when you're happy with them."
        )]

    if text in scholarship_commands:
        return handle_get_scholarships(student_id)

    if profile and profile.get("last_scholarship_id") and text:
        digest_terms = ["digest", "update", "what's new", "show me", "opportunities", "events", "inbox"]
        help_terms = ["hello", "hi", "hey", "start", "help"]
        if not any(kw in text for kw in digest_terms + help_terms):
            return handle_text_pasted(student_id, message.get("text") or "")

    if any(kw in text for kw in ["digest", "update", "what's new", "show me", "opportunities", "events", "inbox"]):
        return handle_get_digest(student_id)

    if any(kw in text for kw in ["draft", "apply", "application"]):
        return [_text_response(
            "Which scholarship would you like to draft? "
            "Tap 'Start Draft' on any scholarship card in your digest, "
            "or tell me the scholarship name."
        )]

    if any(kw in text for kw in ["hello", "hi", "hey", "start", "help"]):
        if not profile or not profile.get("onboarding_complete"):
            card = _build_onboarding_card()
            return [_card_response(
                "Hi! I'm your HKU Campus Agent. I find scholarships, competitions, "
                "and opportunities tailored to you, and help you apply. Let's get you set up:",
                card
            )]
        else:
            return handle_get_digest(student_id)

    # Default — onboard new users, otherwise ask for clarification.
    if not profile or not profile.get("onboarding_complete"):
        card = _build_onboarding_card()
        return [_card_response(
            "Hi! I'm your HKU Campus Agent. I find scholarships, competitions, "
            "and opportunities tailored to you, and help you apply. Let's get you set up:",
            card
        )]

    return [_text_response(
        "I didn't quite understand that.\n\n"
        "Did you mean to update your profile, add a class to your timetable, or see your daily digest?"
    )]

# ---------------------------------------------------------------------------
# Local test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    mock_profile = {
        "id": "local_mock_student",
        "name": "Local Mock Student",
        "email": "local.mock.student@connect.hku.hk",
        "onboarding_complete": False,
    }
    get_profile = lambda student_id: mock_profile
    student_id = sys.argv[1] if len(sys.argv) > 1 else mock_profile["id"]

    print(f"\nSimulating Copilot Chat for {student_id}...")
    print("="*60)

    # Simulate a "hi" message
    responses = handle_message(student_id, {"type": "message", "text": "hi"})
    for r in responses:
        print(f"\n[Response type: {r.get('type')}]")
        if r.get("text"):
            print(r["text"])
        if r.get("attachments"):
            print(f"[Adaptive Card attached: {len(r['attachments'])} card(s)]")
