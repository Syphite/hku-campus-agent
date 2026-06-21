"""
agent/handler.py
Copilot Chat entry point for the HKU Campus Agent.
"""
import os
import json
import logging
import re
from copy import deepcopy
from datetime import datetime, timezone, timedelta
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
from agent.profile  import get_profile, save_profile, build_profile_from_form, update_profile_fields, extract_cv_text, get_graph_access_token, save_graph_token, clear_pending_graph_command
from agent.matching import run_matching
from agent.drafter  import extract_application_questions, generate_draft_answers
from agent.question_extractor import extract_questions_from_file, extract_text_from_application_file
from agent.form_filler import fill_application_form
from agent.application.form_ai import (
    build_filled_data,
    build_gap_overview,
    build_profile_suggestions_for_gaps,
    build_profile_suggestions_overview,
    build_section_prompt,
    consolidate_collection_gaps,
    detect_gaps,
    draft_long_text,
    gap_data_keys,
    merge_filled_data,
    parse_application_collection,
    parse_list_entries_batch,
    safe_max_rows,
)
from agent.application.fill_orchestrator import fill_application
from agent.application.form_planner import build_form_plan, plan_has_fill_targets
from agent.application.state import (
    clear_application_state,
    get_application_state,
    init_application_state,
    update_application_state,
)
from agent.digest   import assemble_digest, format_digest_message

# Import event pipeline
from agent.events.event_extractor  import extract_events_for_student
from agent.conflict_checker        import run_conflict_checks_batch

# Import email pipeline
from agent.email_pipeline import run_inbox_pipeline
from agent.graph import (
    GraphApiError,
    calendar_events_to_blocked_slots,
    create_calendar_event,
    get_calendar_events,
)

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
        "notification_preference": preferences.get("notification_preference", "daily_morning"),
        "consent_inbox": str(bool(profile.get("consent", {}).get("inbox"))).lower(),
        "consent_calendar": str(bool(profile.get("consent", {}).get("calendar"))).lower(),
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

def _looks_like_calendar_add_message(text: str) -> bool:
    lowered = (text or "").lower()
    return any(
        phrase in lowered
        for phrase in (
            "add to my calendar",
            "put on my calendar",
            "add to calendar",
            "put on calendar",
            "schedule ",
        )
    )

def _looks_like_event_registration_message(text: str) -> bool:
    lowered = (text or "").lower()
    return any(
        phrase in lowered
        for phrase in ("register for", "sign up for", "sign me up for", "join ")
    )

def _has_calendar_consent(profile: dict | None) -> bool:
    return bool((profile or {}).get("consent", {}).get("calendar"))

def _has_inbox_consent(profile: dict | None) -> bool:
    return bool((profile or {}).get("consent", {}).get("inbox"))

GRAPH_OAUTH_CONNECTION = os.getenv("GRAPH_OAUTH_CONNECTION", "GraphOAuth")


def _oauth_login_required(text: str, pending_command: str | None = None) -> dict:
    return {
        "type": "oauth_login_required",
        "connection_name": GRAPH_OAUTH_CONNECTION,
        "text": text,
        "pending_command": pending_command,
    }


def _require_graph_token(profile: dict, *, needs_inbox: bool = False, needs_calendar: bool = False) -> dict | None:
    if not needs_inbox and not needs_calendar:
        return None
    if get_graph_access_token(profile):
        return None
    parts = []
    if needs_inbox:
        parts.append("emails")
    if needs_calendar:
        parts.append("calendar")
    scope = " and ".join(parts)
    return _oauth_login_required(
        f"To access your {scope}, I need your permission. Please sign in with your Microsoft account.",
        pending_command="digest" if needs_inbox else None,
    )


def handle_graph_token_response(student_id: str, token_payload) -> list:
    """Save OAuth token and optionally replay the command that triggered sign-in."""
    if not save_graph_token(student_id, token_payload):
        return [_text_response("Sign-in did not complete — no token was received. Please try again.")]

    pending = clear_pending_graph_command(student_id)
    responses = [_text_response("Successfully signed in! I now have access to your emails and calendar.")]
    if pending == "digest":
        digest_result = handle_get_digest(student_id)
        if isinstance(digest_result, dict):
            responses.append(_text_response(digest_result.get("text", "Please try **digest** again.")))
        else:
            responses.extend(digest_result)
    else:
        responses.append(_text_response("Type **digest** when you're ready for your update."))
    return responses

_WEEKDAY_NAMES = (
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
)
_WEEKDAY_INDEX = {day.lower(): index for index, day in enumerate(_WEEKDAY_NAMES)}

def _hk_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))

def _merge_blocked_slots(manual_slots: list, calendar_slots: list) -> list:
    """Merge manual timetable rows with calendar imports; manual rows win on duplicates."""
    seen = {
        (slot.get("day", "").lower(), slot.get("start"), slot.get("end"))
        for slot in manual_slots or []
    }
    merged = list(manual_slots or [])
    for slot in calendar_slots or []:
        key = (slot.get("day", "").lower(), slot.get("start"), slot.get("end"))
        if key in seen:
            continue
        merged.append(slot)
        seen.add(key)
    return merged

def _prefill_timetable_from_calendar(profile: dict, manual_slots: list, consent_calendar: bool) -> tuple[list, int]:
    """Import blocked_slots from Outlook when calendar consent is granted."""
    if not consent_calendar:
        return manual_slots, 0

    user_token = get_graph_access_token(profile)
    if not user_token:
        return manual_slots, 0

    now = _hk_now()
    start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    end_dt = (now + timedelta(days=120)).replace(hour=23, minute=59, second=59).isoformat()
    result = get_calendar_events(user_token, start_dt, end_dt)
    if not result.get("success"):
        logger.warning(f"Calendar prefill failed: {result.get('error')}")
        return manual_slots, 0

    calendar_slots = calendar_events_to_blocked_slots(result.get("events", []))
    merged = _merge_blocked_slots(manual_slots, calendar_slots)
    imported_count = max(0, len(merged) - len(manual_slots or []))
    return merged, imported_count

def _event_to_calendar_times(event: dict) -> dict | None:
    """Derive concrete calendar start/end datetimes for an extracted event."""
    sessions = event.get("event_sessions") or []
    deadline = event.get("deadline")
    now = _hk_now()

    if sessions:
        session = sessions[0]
        day_name = (session.get("day") or "").strip()
        start_time = _format_time_value(session.get("start", "09:00"))
        end_time = _format_time_value(session.get("end", "10:00"))
        target_weekday = _WEEKDAY_INDEX.get(day_name.lower())
        if target_weekday is None:
            return None
        days_ahead = (target_weekday - now.weekday()) % 7
        if days_ahead == 0 and now.strftime("%H:%M") >= start_time:
            days_ahead = 7
        event_date = (now + timedelta(days=days_ahead)).date()
        return {
            "start_iso": f"{event_date.isoformat()}T{start_time}:00",
            "end_iso": f"{event_date.isoformat()}T{end_time}:00",
            "display_date": event_date.strftime("%A, %d %B %Y"),
            "display_time": f"{start_time}–{end_time}",
        }

    if deadline:
        try:
            event_date = datetime.strptime(str(deadline)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
        return {
            "start_iso": f"{event_date.isoformat()}T09:00:00",
            "end_iso": f"{event_date.isoformat()}T10:00:00",
            "display_date": event_date.strftime("%A, %d %B %Y"),
            "display_time": "09:00–10:00",
        }
    return None

def _format_event_datetime_display(start_iso: str, end_iso: str) -> str:
    try:
        start_dt = datetime.fromisoformat(start_iso.split(".")[0])
        end_dt = datetime.fromisoformat(end_iso.split(".")[0])
        return (
            f"{start_dt.strftime('%A, %d %B %Y')} "
            f"({start_dt.strftime('%H:%M')}–{end_dt.strftime('%H:%M')})"
        )
    except ValueError:
        return start_iso

def _parse_calendar_date(date_text: str) -> str | None:
    """Parse a natural-language or ISO date into YYYY-MM-DD."""
    raw = (date_text or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw

    now = _hk_now()
    lowered = raw.lower()
    for day_name in _WEEKDAY_NAMES:
        if day_name.lower() in lowered or day_name[:3].lower() in lowered.split():
            target = _WEEKDAY_INDEX[day_name.lower()]
            days_ahead = (target - now.weekday()) % 7
            if days_ahead == 0 and "next" in lowered:
                days_ahead = 7
            return (now + timedelta(days=days_ahead)).date().isoformat()

    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%B %d %Y", "%d %B %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None

def _calendar_data_to_iso(data: dict) -> tuple[str, str] | None:
    date_value = _parse_calendar_date(data.get("date", ""))
    start_time = _format_time_value(data.get("start_time", ""))
    end_time = _format_time_value(data.get("end_time", ""))
    if not date_value or not start_time or not end_time:
        return None
    return (
        f"{date_value}T{start_time}:00",
        f"{date_value}T{end_time}:00",
    )

def _missing_calendar_fields(data: dict) -> list[str]:
    missing = []
    if not str(data.get("title", "") or "").strip():
        missing.append("title")
    if not _parse_calendar_date(str(data.get("date", "") or "")):
        missing.append("date")
    if not _format_time_value(data.get("start_time", "")):
        missing.append("start_time")
    if not _format_time_value(data.get("end_time", "")):
        missing.append("end_time")
    return missing

def _calendar_missing_prompt(data: dict, missing_fields: list[str]) -> str:
    labels = {
        "title": "event title",
        "date": "date",
        "start_time": "start time",
        "end_time": "end time",
    }
    known = []
    if data.get("title"):
        known.append(f"title: {data['title']}")
    if data.get("date"):
        known.append(f"date: {data['date']}")
    if data.get("start_time"):
        known.append(f"start: {data['start_time']}")
    if data.get("end_time"):
        known.append(f"end: {data['end_time']}")
    missing_text = ", ".join(labels.get(field, field) for field in missing_fields)
    prefix = f"I have {', '.join(known)}. " if known else ""
    return f"{prefix}Please tell me the {missing_text} for this calendar event."

def _save_pending_calendar_add(profile: dict, data: dict, response_text: str | None = None) -> list:
    missing_fields = _missing_calendar_fields(data)
    profile["pending_action"] = "complete_calendar_add"
    profile["pending_data"] = data
    save_profile(profile)
    return [_text_response(response_text or _calendar_missing_prompt(data, missing_fields))]

def _handle_pending_calendar_add(profile: dict, text: str) -> list:
    try:
        parsed = {}
        try:
            parsed = _parse_profile_update(text)
        except Exception as exc:
            logger.warning(f"Pending calendar parser fallback: {exc}")
        new_data = _merge_timetable_data(
            parsed.get("extracted_data", {}) or {},
            _extract_calendar_fields_locally(text),
        )
        data = _merge_timetable_data(profile.get("pending_data", {}), new_data)
        missing_fields = _missing_calendar_fields(data)
        if missing_fields:
            return _save_pending_calendar_add(
                profile,
                data,
                parsed.get("agent_response") or _calendar_missing_prompt(data, missing_fields),
            )

        _clear_pending_state(profile)
        save_profile(profile)
        card = _calendar_add_confirmation_card(data)
        return [_card_response("Please confirm this calendar event.", card)]
    except Exception as exc:
        logger.error(f"Pending calendar add error: {exc}")
        return [_text_response("I had trouble completing that calendar event. Please send the details again.")]

def _extract_calendar_fields_locally(text: str) -> dict:
    data = {}
    raw_text = text or ""
    lowered = raw_text.lower()

    time_match = re.search(
        r"(?:from\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*(?:to|-)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
        lowered,
    )
    if time_match:
        data["start_time"] = _format_time_value(time_match.group(1))
        data["end_time"] = _format_time_value(time_match.group(2))
    else:
        at_match = re.search(r"at\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", lowered)
        if at_match:
            data["start_time"] = _format_time_value(at_match.group(1))

    for day_name in _WEEKDAY_NAMES:
        if day_name.lower() in lowered or day_name[:3].lower() in lowered.split():
            data["date"] = day_name
            break

    iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", raw_text)
    if iso_match:
        data["date"] = iso_match.group(1)

    title_match = re.search(r"(?:called|named|titled)\s+(.+?)(?:\s+on|\s+at|$)", raw_text, re.IGNORECASE)
    if title_match:
        data["title"] = title_match.group(1).strip(" .")
    return data

def _calendar_add_confirmation_card(data: dict) -> dict:
    title = data.get("title", "Event")
    date_value = data.get("date", "")
    start_time = _format_time_value(data.get("start_time", ""))
    end_time = _format_time_value(data.get("end_time", ""))
    location = str(data.get("location", "") or "").strip()
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": (
                    f"I will add **{title}** to your calendar on **{date_value}** "
                    f"at **{start_time}–{end_time}**. Confirm?"
                ),
                "wrap": True,
            }
        ],
        "actions": [
            {
                "type": "Action.Submit",
                "title": "✅ Yes",
                "style": "positive",
                "data": {
                    "action": "confirm_calendar_add",
                    "title": title,
                    "date": date_value,
                    "start_time": start_time,
                    "end_time": end_time,
                    "location": location,
                },
            },
            {
                "type": "Action.Submit",
                "title": "❌ No",
                "data": {"action": "cancel_calendar_add"},
            },
        ],
    }

def _event_registration_confirmation_card(event_data: dict) -> dict:
    title = event_data.get("title", "Event")
    display_date = event_data.get("display_date", "")
    display_time = event_data.get("display_time", "")
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": f"Confirm registration for **{title}** on **{display_date}** at **{display_time}**?",
                "wrap": True,
            }
        ],
        "actions": [
            {
                "type": "Action.Submit",
                "title": "✅ Yes",
                "style": "positive",
                "data": {
                    "action": "confirm_event_registration",
                    "event_id": event_data.get("event_id", ""),
                    "title": title,
                    "location": event_data.get("location", ""),
                    "start_iso": event_data.get("start_iso", ""),
                    "end_iso": event_data.get("end_iso", ""),
                    "display_date": display_date,
                    "display_time": display_time,
                },
            },
            {
                "type": "Action.Submit",
                "title": "❌ No",
                "data": {"action": "cancel_event_registration"},
            },
        ],
    }

def _store_shown_events(profile: dict, events: list) -> None:
    stored = []
    for event in events or []:
        cal_times = _event_to_calendar_times(event)
        if not cal_times:
            continue
        stored.append({
            "event_id": event.get("source_id") or event.get("id", ""),
            "title": event.get("title", ""),
            "location": event.get("location", ""),
            **cal_times,
        })
    profile["last_shown_events"] = stored[:20]

def _find_event_for_registration(profile: dict, text: str) -> dict | None:
    query = (text or "").lower()
    for prefix in ("register for", "sign up for", "sign me up for", "join"):
        if prefix in query:
            query = query.split(prefix, 1)[1].strip(" :")
            break
    if not query:
        return None

    candidates = profile.get("last_shown_events") or []
    best = None
    for candidate in candidates:
        title = str(candidate.get("title", "")).lower()
        if title and (title in query or query in title):
            best = candidate
            break
    if best:
        return best

    try:
        events = extract_events_for_student(profile.get("id", ""))
    except Exception as exc:
        logger.error(f"Event lookup for registration failed: {exc}")
        return None

    for event in events:
        title = str(event.get("title", "")).lower()
        if title and (title in query or query in title):
            cal_times = _event_to_calendar_times(event)
            if not cal_times:
                continue
            return {
                "event_id": event.get("source_id") or event.get("id", ""),
                "title": event.get("title", ""),
                "location": event.get("location", ""),
                **cal_times,
            }
    return None

def handle_start_event_registration(event_data: dict) -> list:
    if not event_data.get("start_iso") or not event_data.get("end_iso"):
        return [_text_response("I couldn't determine the event schedule for registration.")]
    card = _event_registration_confirmation_card(event_data)
    return [_card_response("Please confirm your registration.", card)]

def handle_confirm_event_registration(student_id: str, value: dict) -> list:
    profile = get_profile(student_id)
    if not profile:
        return [_text_response("Please complete onboarding first.")]

    event_id = value.get("event_id", "")
    title = value.get("title", "Event")
    start_iso = value.get("start_iso", "")
    end_iso = value.get("end_iso", "")
    location = value.get("location", "")

    registered = list(profile.get("registered_events") or [])
    if event_id and event_id not in registered:
        registered.append(event_id)
        profile["registered_events"] = registered

    if not _has_calendar_consent(profile):
        save_profile(profile)
        return [_text_response(
            "Registration noted. Enable calendar access in your profile settings to auto-add events."
        )]

    auth_required = _require_graph_token(profile, needs_calendar=True)
    if auth_required:
        save_profile(profile)
        return [auth_required]

    user_token = get_graph_access_token(profile)
    result = create_calendar_event(user_token, title, start_iso, end_iso, location)
    save_profile(profile)
    if result.get("success"):
        display = _format_event_datetime_display(start_iso, end_iso)
        return [_text_response(f"✅ Added {title} to your calendar for {display}.")]
    logger.error(f"Event registration calendar create failed: {result.get('error')}")
    return [_text_response("I couldn't reach your calendar right now. Please try again later.")]

def handle_confirm_calendar_add(student_id: str, value: dict) -> list:
    profile = get_profile(student_id)
    if not profile:
        return [_text_response("Please complete onboarding first.")]

    if not _has_calendar_consent(profile):
        return [_text_response(
            "Calendar access is not enabled. Turn on calendar permission in your profile settings first."
        )]

    iso_times = _calendar_data_to_iso(value)
    if not iso_times:
        return [_text_response("I couldn't parse that calendar event. Please try again.")]

    start_iso, end_iso = iso_times
    title = value.get("title", "Event")
    location = value.get("location", "")
    auth_required = _require_graph_token(profile, needs_calendar=True)
    if auth_required:
        return [auth_required]

    user_token = get_graph_access_token(profile)
    result = create_calendar_event(user_token, title, start_iso, end_iso, location)
    if result.get("success"):
        display = _format_event_datetime_display(start_iso, end_iso)
        return [_text_response(f"✅ Added {title} to your calendar for {display}.")]
    logger.error(f"Manual calendar create failed: {result.get('error')}")
    return [_text_response("I couldn't reach your calendar right now. Please try again later.")]

def handle_event_registration(student_id: str, text: str) -> list:
    profile = get_profile(student_id)
    if not profile:
        return [_text_response("Please complete onboarding first.")]

    event_data = _find_event_for_registration(profile, text)
    if not event_data:
        return [_text_response(
            "I couldn't find that event. Try 'events' or your digest first, then say "
            "'register for [event name]'."
        )]
    return handle_start_event_registration(event_data)

PROFILE_UPDATE_SYSTEM_PROMPT = """You are an intelligent university campus agent. Analyze the user's message to determine their intent and extract relevant information.

You can handle these main intents:
1. "update_profile": Changing personal details. Valid fields: name, faculty, programme, year_of_study, local_status, gpa, financial_need, notification_preference, module_scholarships (bool), module_events (bool), module_inbox (bool).
2. "update_interests": Adding or removing items from the user's 'interests' list.
3. "update_activities": Adding or removing items from the user's 'activities' list.
4. "add_timetable": Adding a class to their schedule (requires: course_code, day, start_time, end_time).
5. "add_calendar_event": Adding an arbitrary event to Outlook calendar (requires: title, date, start_time, end_time; location optional).

Return ONLY a raw JSON object with this exact structure:
{
  "intent": "update_profile" | "update_interests" | "update_activities" | "add_timetable" | "add_calendar_event" | "unknown",
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
- If the user says "add team meeting to my calendar on Friday at 2pm until 4pm", intent is "add_calendar_event", extracted_data has title="team meeting", date="Friday", start_time="14:00", end_time="16:00".
- If the user says "add study group to my calendar on Monday", intent is "add_calendar_event", extracted_data has title="study group", date="Monday", missing_fields are ["start_time", "end_time"].
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

    if event.get("match_reason"):
        body.append({
            "type": "TextBlock",
            "text": f"**Why this matches:** {event['match_reason']}",
            "wrap": True,
            "size": "Small",
            "color": "Good",
        })

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

    cal_times = _event_to_calendar_times(event)
    if cal_times:
        actions.append({
            "type": "Action.Submit",
            "title": "Register",
            "data": {
                "action": "start_event_registration",
                "event_id": event.get("source_id") or event.get("id", ""),
                "title": event.get("title", "Event"),
                "location": event.get("location", ""),
                "start_iso": cal_times["start_iso"],
                "end_iso": cal_times["end_iso"],
                "display_date": cal_times["display_date"],
                "display_time": cal_times["display_time"],
            }
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

    consent_inbox = str(form_data.get("consent_inbox", "false")).lower() == "true"
    consent_calendar = str(form_data.get("consent_calendar", "false")).lower() == "true"
    # In production, consent_inbox/consent_calendar would trigger the OAuth2 delegated-consent flow.
    # This prototype stores consent intent only.

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

    blocked_slots, imported_count = _prefill_timetable_from_calendar(
        {"email": form_data.get("email", f"{student_id}@connect.hku.hk")},
        blocked_slots,
        consent_calendar,
    )

    # Inject timetable into profile before saving
    profile["timetable"] = {
        "blocked_slots": blocked_slots,
        "upcoming_deadlines": []
    }
    profile["consent"] = {
        "inbox": consent_inbox,
        "calendar": consent_calendar,
    }

    profile["id"] = student_id
    save_profile(profile)

    calendar_note = ""
    if imported_count:
        calendar_note = f"\n\nImported {imported_count} class(es) from your Outlook calendar."

    responses = [_text_response(
        f"Profile set up! Welcome, {profile.get('name', 'Student')}. I've saved your preferences.{calendar_note} "
        "What would you like to do now?\n\n"
        "• Type 'digest' for your daily update\n"
        "• Type 'scholarships' to browse matches\n"
        "• Type 'events' to see upcoming competitions and events\n"
        "• Type 'inbox' to review your archived emails\n"
        "• Type 'help' anytime to see all commands"
    )]
    if (consent_inbox or consent_calendar) and not get_graph_access_token(profile):
        responses.append(_oauth_login_required(
            "To connect your email and calendar, please sign in with your Microsoft account once during registration.",
            pending_command=None,
        ))
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

    auth_required = _require_graph_token(
        profile,
        needs_inbox="inbox" in modules and _has_inbox_consent(profile),
    )
    if auth_required:
        profile["pending_graph_command"] = auth_required.get("pending_command") or "digest"
        save_profile(profile)
        return auth_required

    # 1. Run email inbox pipeline only when enabled and consented
    inbox_summary = None
    inbox_error_note = None
    if "inbox" in modules and _has_inbox_consent(profile):
        try:
            inbox_summary = run_inbox_pipeline(
                student_id,
                profile,
                user_token=get_graph_access_token(profile),
            )
        except GraphApiError as exc:
            logger.error("Email pipeline Graph error: %s", exc.to_log_dict())
            inbox_summary = {
                "processed": 0,
                "archived": 0,
                "kept": 0,
                "archived_items": [],
                "relevant_items": [],
                "urgent_items": [],
                "ambiguous_items": [],
                "error": "Inbox unavailable — check Graph diagnostics in logs.",
                "graph_hint": exc.hint,
            }
            inbox_error_note = exc.hint or "Inbox unavailable — check Graph diagnostics in logs."
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
            _store_shown_events(profile, checked_events)
            save_profile(profile)
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

    if inbox_error_note:
        responses.append(_text_response(f"📬 **Inbox:** {inbox_error_note}"))

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

def handle_get_events(student_id: str) -> list:
    """Run event extraction and render only event sections."""
    profile = get_profile(student_id)
    if not profile:
        return [_card_response("I don't have your profile yet. Let's get you set up:", _build_onboarding_card())]

    modules = profile.get("preferences", {}).get("modules_enabled", ["scholarships", "events", "inbox"])
    if "events" not in modules:
        return [_text_response("Event matching is currently turned off in your profile settings.")]

    try:
        raw_events = extract_events_for_student(student_id)
        checked_events = run_conflict_checks_batch(raw_events, profile)
        _store_shown_events(profile, checked_events)
        save_profile(profile)
    except Exception as e:
        logger.error(f"Event pipeline error: {e}")
        checked_events = []

    digest = assemble_digest(
        student_id=student_id,
        scholarship_result={"apply_now": [], "prepare": []},
        events=checked_events,
        inbox_summary={"processed": 0, "archived": 0, "kept": 0, "archived_items": [], "relevant_items": []},
    )
    urgent = digest["events"]["urgent"]
    upcoming = digest["events"]["upcoming"]

    if not urgent and not upcoming:
        return [_text_response(
            "No matching events right now — check back after the next feed refresh, or type **digest** for your full update."
        )]

    lines = ["Here are your event matches:\n"]
    if urgent:
        lines.append(f"🏆 **{len(urgent)} event(s) with upcoming deadlines**")
    if upcoming:
        lines.append(f"📌 **{len(upcoming)} upcoming event(s)** worth bookmarking")
    responses = [_text_response("\n".join(lines))]

    if urgent:
        responses.append(_text_response("**🏆 Events — Closing Soon:**"))
        for event in urgent[:10]:
            card = _event_card(event)
            responses.append({
                "type": "message",
                "text": "Event opportunity",
                "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]
            })

    if upcoming:
        responses.append(_text_response("**📌 Upcoming Events:**"))
        for event in upcoming[:10]:
            card = _event_card(event)
            responses.append({
                "type": "message",
                "text": "Event opportunity",
                "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]
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


def _collection_gaps(gaps: list) -> list:
    return consolidate_collection_gaps(gaps)


def _section_storage_id(gap: dict) -> str:
    return gap.get("section_id") or gap.get("key") or ""


def _gap_for_section_id(state: dict, section_id: str) -> dict | None:
    if not section_id:
        return None
    for gap in _collection_gaps(state.get("gap_queue", [])):
        if _section_storage_id(gap) == section_id or gap.get("key") == section_id:
            return gap
    return None


def _store_gap_value(mapping: dict, gap: dict, value) -> None:
    for key in gap_data_keys(gap):
        mapping[key] = value


def _long_text_gaps(gaps: list) -> list:
    return [gap for gap in gaps or [] if gap.get("type") == "long_text"]


def _looks_like_section_advance(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    if normalized in {
        "next", "done", "move on", "skip", "no more", "continue",
        "that's all", "thats all", "move to next section", "pass",
        "nothing to add", "none", "n/a",
    }:
        return True
    phrases = (
        "skip", "next", "move on", "pass", "no more",
        "don't have", "dont have", "nothing to add", "don't have any",
    )
    return any(phrase in normalized for phrase in phrases)


def _looks_like_collection_review(text: str) -> bool:
    normalized = (text or "").strip().lower()
    return normalized in {"review", "finish", "done collecting", "go to review"} or "review now" in normalized


def _looks_like_collection_cancel(text: str) -> bool:
    normalized = (text or "").strip().lower()
    return normalized in {
        "cancel application", "stop application", "cancel form", "stop form",
    }


def _gap_is_complete(state: dict, gap: dict) -> bool:
    keys = gap_data_keys(gap)
    if not keys:
        return False
    skipped = set(state.get("skipped_sections") or [])
    if any(key in skipped for key in keys):
        return True
    gap_type = gap.get("type")
    if gap_type == "repeating_list":
        pending = state.get("pending_list_data") or {}
        return any(pending.get(key) for key in keys)
    if gap_type == "long_text":
        drafts = state.get("long_text_drafts") or {}
        return any(drafts.get(key) for key in keys)
    if gap_type == "free_field":
        pending = state.get("pending_free_fields") or {}
        return any(pending.get(key) for key in keys)
    return False


def _next_unfilled_gap(state: dict, after_gap: dict | None = None) -> dict | None:
    collection_gaps = _collection_gaps(state.get("gap_queue", []))
    start_index = 0
    if after_gap:
        after_id = _section_storage_id(after_gap)
        found = False
        for index, gap in enumerate(collection_gaps):
            if _section_storage_id(gap) == after_id:
                start_index = index + 1
                found = True
                break
        if not found:
            start_index = 0

    for gap in collection_gaps[start_index:]:
        if not _gap_is_complete(state, gap):
            return gap
    return None


def _mark_suggestion_reviewed(profile: dict, section_key: str) -> None:
    reviewed = list(get_application_state(profile).get("suggestions_reviewed") or [])
    if section_key and section_key not in reviewed:
        reviewed.append(section_key)
    update_application_state(profile, suggestions_reviewed=reviewed)


def _profile_suggestions_card(gap: dict, suggestions: list) -> dict:
    label = gap.get("label") or gap.get("key", "Section").replace("_", " ").title()
    section_id = _section_storage_id(gap)
    body = [
        {
            "type": "TextBlock",
            "text": f"Include these from your profile for **{label}**?",
            "weight": "Bolder",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": "I matched activities from your profile to this section. You can accept, enter your own entries, or skip.",
            "wrap": True,
            "size": "Small",
        },
    ]
    for index, entry in enumerate(suggestions[:5], start=1):
        source = entry.get("_source_text") or ""
        summary = ", ".join(
            str(value)
            for key, value in entry.items()
            if value and not str(key).startswith("_")
        )
        line = source or summary
        if line:
            body.append({
                "type": "TextBlock",
                "text": f"{index}. {line}",
                "wrap": True,
            })
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
        "actions": [
            {
                "type": "Action.Submit",
                "title": "Yes, include these",
                "style": "positive",
                "data": {
                    "action": "accept_profile_suggestions",
                    "section_key": section_id,
                },
            },
            {
                "type": "Action.Submit",
                "title": "No, I'll enter my own",
                "data": {
                    "action": "reject_profile_suggestions",
                    "section_key": section_id,
                },
            },
            {
                "type": "Action.Submit",
                "title": "Skip this section",
                "data": {
                    "action": "skip_application_section",
                    "section_key": section_id,
                },
            },
        ],
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
    pending_free_fields = state.get("pending_free_fields") or {}
    skipped_sections = set(state.get("skipped_sections") or [])
    ai_drafted_keys = set(state.get("ai_drafted_keys") or [])

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
                if value and not str(field).startswith("_")
            )
            body.append({
                "type": "TextBlock",
                "text": f"{index}. {summary}",
                "wrap": True,
            })

    for gap in _collection_gaps(state.get("gap_queue", [])):
        key = gap.get("key")
        if key not in skipped_sections:
            continue
        label = gap.get("label") or key.replace("_", " ").title()
        body.append({
            "type": "TextBlock",
            "text": f"{label} (skipped)",
            "wrap": True,
            "color": "Warning",
            "spacing": "Medium",
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
        suffix = "\n\n_(AI draft)_" if key in ai_drafted_keys else ""
        body.append({
            "type": "TextBlock",
            "text": f"{preview}{suffix}",
            "wrap": True,
        })

    for key, text in pending_free_fields.items():
        if not text or key in long_text_drafts:
            continue
        body.append({
            "type": "TextBlock",
            "text": key.replace("_", " ").title(),
            "weight": "Bolder",
            "wrap": True,
            "spacing": "Medium",
        })
        preview = text if len(text) <= 1200 else f"{text[:1200]}..."
        suffix = "\n\n_(AI draft)_" if key in ai_drafted_keys else ""
        body.append({
            "type": "TextBlock",
            "text": f"{preview}{suffix}",
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


def _application_section_card(gap: dict) -> dict:
    """Optional card for the current collection section with a skip button."""
    label = gap.get("label") or gap.get("key", "Section").replace("_", " ").title()
    return {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": build_section_prompt(gap),
                "wrap": True,
            }
        ],
        "actions": [
            {
                "type": "Action.Submit",
                "title": "Skip this section",
                "data": {
                    "action": "skip_application_section",
                    "section_key": _section_storage_id(gap),
                },
            }
        ],
    }


def _section_prompt_response(profile: dict, gap: dict, state: dict | None = None) -> list:
    state = state or {}
    section_id = _section_storage_id(gap)
    reviewed = set(state.get("suggestions_reviewed") or [])
    suggestions = (state.get("profile_suggestions") or {}).get(section_id) or []
    if gap.get("type") == "repeating_list" and suggestions and section_id not in reviewed:
        return [_card_response("Include from profile?", _profile_suggestions_card(gap, suggestions))]

    return [
        _card_response("Current section", _application_section_card(gap)),
    ]


def _mark_section_skipped(profile: dict, state: dict, gap: dict) -> dict:
    skipped = list(state.get("skipped_sections") or [])
    reviewed = list(state.get("suggestions_reviewed") or [])
    section_id = _section_storage_id(gap)
    for key in gap_data_keys(gap):
        if key and key not in skipped:
            skipped.append(key)
    if section_id and section_id not in reviewed:
        reviewed.append(section_id)
    update_application_state(profile, skipped_sections=skipped, suggestions_reviewed=reviewed)
    return get_application_state(profile)


def _accept_profile_suggestions(profile: dict, state: dict, section_id: str) -> list:
    suggestions = list((state.get("profile_suggestions") or {}).get(section_id) or [])
    if not suggestions:
        _mark_suggestion_reviewed(profile, section_id)
        current_gap = state.get("current_gap") or {}
        return _section_prompt_response(profile, current_gap, get_application_state(profile))

    cleaned = []
    for entry in suggestions:
        cleaned.append({k: v for k, v in entry.items() if not str(k).startswith("_")})

    gap = _gap_for_section_id(state, section_id) or state.get("current_gap") or {}
    pending_list_data = dict(state.get("pending_list_data") or {})
    _store_gap_value(pending_list_data, gap, cleaned)
    _mark_suggestion_reviewed(profile, section_id)
    update_application_state(profile, pending_list_data=pending_list_data)
    save_profile(profile)

    label = gap.get("label") or section_id.replace("_", " ")
    responses = [_text_response(f"Added {len(cleaned)} entr{'y' if len(cleaned) == 1 else 'ies'} from your profile for **{label}**.")]
    responses.extend(_advance_application_gap(profile, get_application_state(profile)))
    return responses


def _reject_profile_suggestions(profile: dict, state: dict, section_id: str) -> list:
    _mark_suggestion_reviewed(profile, section_id)
    state = get_application_state(profile)
    current_gap = state.get("current_gap") or {}
    if _section_storage_id(current_gap) != section_id:
        matched = _gap_for_section_id(state, section_id)
        if matched:
            current_gap = matched
    responses = [_text_response("No problem — paste your entries when you're ready.")]
    responses.extend(_section_prompt_response(profile, current_gap, state))
    return responses


def _skip_current_section(profile: dict, state: dict, response_text: str | None = None) -> list:
    current = state.get("current_gap") or {}
    state = _mark_section_skipped(profile, state, current)
    responses = _advance_application_gap(profile, state)
    if response_text and responses:
        responses.insert(0, _text_response(response_text))
    return responses


def _draft_current_section(profile: dict, state: dict, current_gap: dict) -> list:
    gap_type = current_gap.get("type")
    if gap_type not in ("long_text", "free_field"):
        return [_text_response("I can only draft essay or open-answer sections. Please paste your entries instead.")]

    key = current_gap.get("key")
    schema = dict(current_gap.get("schema") or {})
    schema.setdefault("anchor_label", current_gap.get("label", key))
    schema.setdefault("target_words", 800)
    draft = draft_long_text(schema, profile, state.get("pending_list_data") or {})

    long_text_drafts = dict(state.get("long_text_drafts") or {})
    pending_free_fields = dict(state.get("pending_free_fields") or {})
    ai_drafted_keys = list(state.get("ai_drafted_keys") or [])

    if gap_type == "long_text":
        long_text_drafts = dict(state.get("long_text_drafts") or {})
        _store_gap_value(long_text_drafts, current_gap, draft)
    else:
        pending_free_fields = dict(state.get("pending_free_fields") or {})
        _store_gap_value(pending_free_fields, current_gap, draft)
    for key in gap_data_keys(current_gap):
        if key and key not in ai_drafted_keys:
            ai_drafted_keys.append(key)

    update_application_state(
        profile,
        long_text_drafts=long_text_drafts,
        pending_free_fields=pending_free_fields,
        ai_drafted_keys=ai_drafted_keys,
    )
    save_profile(profile)

    preview = draft if len(draft) <= 400 else f"{draft[:400]}..."
    responses = [_text_response(f"Draft ready for **{current_gap.get('label', key)}**:\n\n{preview}")]
    responses.extend(_advance_application_gap(profile, get_application_state(profile)))
    return responses


def _fill_current_section(profile: dict, state: dict, current_gap: dict, user_text: str, answer_text: str = "") -> list:
    gap_type = current_gap.get("type")
    text_value = (answer_text or user_text or "").strip()
    list_key = current_gap.get("key")

    if gap_type == "repeating_list":
        list_schema = current_gap.get("schema") or {}
        max_rows = safe_max_rows(list_schema)
        entries = parse_list_entries_batch(text_value, list_schema, max_rows)
        if not entries:
            if _looks_like_section_advance(text_value):
                return _skip_current_section(profile, state, "Okay, skipping this section.")
            return [_text_response(build_section_prompt(current_gap))]
        pending_list_data = dict(state.get("pending_list_data") or {})
        _store_gap_value(pending_list_data, current_gap, entries)
        update_application_state(profile, pending_list_data=pending_list_data)
        save_profile(profile)
        label = current_gap.get("label") or list_key.replace("_", " ")
        responses = [_text_response(f"Got {len(entries)} {label} entr{'y' if len(entries) == 1 else 'ies'}.")]
        responses.extend(_advance_application_gap(profile, get_application_state(profile)))
        return responses

    if gap_type == "long_text":
        if not text_value:
            return [_text_response("Please paste your answer, say **draft**, or **skip**.")]
        long_text_drafts = dict(state.get("long_text_drafts") or {})
        _store_gap_value(long_text_drafts, current_gap, text_value)
        update_application_state(profile, long_text_drafts=long_text_drafts)
        save_profile(profile)
        responses = [_text_response(f"Saved your answer for **{current_gap.get('label', list_key)}**.")]
        responses.extend(_advance_application_gap(profile, get_application_state(profile)))
        return responses

    if gap_type == "free_field":
        if not text_value:
            return [_text_response("Please paste your answer, say **draft**, or **skip**.")]
        pending_free_fields = dict(state.get("pending_free_fields") or {})
        _store_gap_value(pending_free_fields, current_gap, text_value)
        update_application_state(profile, pending_free_fields=pending_free_fields)
        save_profile(profile)
        responses = [_text_response(f"Saved your answer for **{current_gap.get('label', list_key)}**.")]
        responses.extend(_advance_application_gap(profile, get_application_state(profile)))
        return responses

    return [_text_response("Say **skip** to move on, or reply with the requested information.")]


def _begin_application_review(profile: dict, state: dict) -> list:
    long_text_drafts = dict(state.get("long_text_drafts") or {})
    skipped = set(state.get("skipped_sections") or [])
    ai_drafted_keys = list(state.get("ai_drafted_keys") or [])

    for gap in _collection_gaps(state.get("gap_queue", [])):
        if gap.get("type") != "long_text":
            continue
        keys = gap_data_keys(gap)
        if any(key in skipped for key in keys):
            continue
        if any(long_text_drafts.get(key) for key in keys):
            continue
        draft = draft_long_text(
            gap["schema"],
            profile,
            state.get("pending_list_data") or {},
        )
        _store_gap_value(long_text_drafts, gap, draft)
        for key in keys:
            if key and key not in ai_drafted_keys:
                ai_drafted_keys.append(key)

    state["long_text_drafts"] = long_text_drafts
    state["ai_drafted_keys"] = ai_drafted_keys
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
    current = state.get("current_gap") or {}
    next_gap = _next_unfilled_gap(state, after_gap=current if current else None)
    if next_gap:
        update_application_state(
            profile,
            current_gap=next_gap,
            step="collecting_list",
        )
        save_profile(profile)
        return _section_prompt_response(profile, next_gap, get_application_state(profile))

    return _begin_application_review(profile, get_application_state(profile))


def handle_application_collection_message(student_id: str, text: str) -> list | None:
    profile = get_profile(student_id)
    if not profile:
        return None

    state = get_application_state(profile)
    if state.get("step") != "collecting_list":
        return None

    if _looks_like_collection_cancel(text):
        return handle_cancel_application_collection(student_id)

    if _looks_like_collection_review(text):
        return _begin_application_review(profile, state)

    current_gap = state.get("current_gap") or {}
    if not current_gap:
        return [_text_response("No section is active right now.")]

    if _looks_like_section_advance(text):
        return _skip_current_section(profile, state, "Okay, skipping this section.")

    parsed = parse_application_collection(text, current_gap, profile, state)
    intent = parsed.get("intent", "unknown")
    agent_response = parsed.get("agent_response") or ""
    extracted = parsed.get("extracted_data") or {}
    answer_text = str(extracted.get("answer_text") or "").strip()

    if intent == "skip_section":
        return _skip_current_section(profile, state, agent_response or None)

    if intent == "draft_section":
        return _draft_current_section(profile, state, current_gap)

    if intent == "fill_section":
        return _fill_current_section(profile, state, current_gap, text, answer_text)

    if intent == "clarify" and agent_response:
        return [_text_response(agent_response)]

    if agent_response:
        return [_text_response(agent_response)]

    return [_text_response(build_section_prompt(current_gap))]


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
        plan = build_form_plan(input_path, stored_content_type, profile)
    except Exception as exc:
        logger.error(f"Application form analysis failed: {exc}")
        return [_text_response(
            "I couldn't analyze that form. Please upload a table-based DOCX or PDF application form."
        )]

    if not plan_has_fill_targets(plan):
        metadata = plan.get("metadata") or {}
        table_count = metadata.get("table_count", 0)
        errors = metadata.get("analysis_errors") or []
        detail = errors[0] if errors else "no fillable fields were detected"
        return [_text_response(
            f"I parsed {table_count} table section(s) but couldn't identify fillable fields ({detail}). "
            "Try a clearer scan/PDF export, or paste missing answers after review."
        )]

    schema = plan.get("table_schema") or {}
    free_fields = plan.get("free_fields") or []
    filled_data = plan.get("filled_data") or build_filled_data(schema, profile, free_fields)
    gaps = plan.get("gaps") or detect_gaps(schema, filled_data, profile, free_fields)
    form_json = plan.get("form_json", "")

    profile.pop("pending_application", None)
    _clear_application_review_state(profile)

    collection_gaps = _collection_gaps(gaps)
    first_gap = _next_unfilled_gap({"gap_queue": gaps, "skipped_sections": [], "pending_list_data": {}, "long_text_drafts": {}, "pending_free_fields": {}}) if collection_gaps else None
    profile_suggestions = build_profile_suggestions_for_gaps(gaps, profile) if collection_gaps else {}
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
        form_plan=plan,
        form_json=form_json,
        gap_queue=gaps,
        current_gap=first_gap,
        pending_list_data={},
        pending_free_fields={},
        long_text_drafts={},
        skipped_sections=[],
        ai_drafted_keys=[],
        profile_suggestions=profile_suggestions,
        suggestions_reviewed=[],
    )
    profile["last_scholarship_id"] = "ss_472"
    profile["last_scholarship_name"] = DEMO_SCHOLARSHIP_472["name"]
    save_profile(profile)

    if first_gap:
        overview = build_gap_overview(schema, filled_data, gaps)
        suggestion_overview = build_profile_suggestions_overview(gaps, profile, profile_suggestions)
        if suggestion_overview:
            overview += f"\n\n{suggestion_overview}"
        metadata = plan.get("metadata") or {}
        if metadata.get("analysis_errors"):
            overview += "\n\n_Note: some sections needed partial analysis._"
        responses = [
            _text_response(overview),
        ]
        responses.extend(_section_prompt_response(profile, first_gap, get_application_state(profile)))
        return responses

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
            "pending_free_fields": {},
            "long_text_drafts": {},
            "form_plan": {},
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
        state.get("pending_free_fields") or {},
    )
    if additional_notes:
        merged_data.setdefault("long_text", {})
        merged_data["long_text"]["additional_notes"] = additional_notes

    form_plan = state.get("form_plan") or {
        "table_schema": state.get("schema") or {},
        "free_fields": [],
    }
    try:
        fill_application(form_plan, input_path, output_path, merged_data, content_type)
    except Exception as exc:
        logger.error(f"Application form fill failed: {exc}")
        return [_text_response(
            "I couldn't fill that form template. Please try uploading the form again."
        )]

    if not os.path.exists(output_path):
        return [_text_response("The filled form could not be generated. Please try again.")]

    try:
        with open(output_path, "rb") as handle:
            file_bytes = handle.read()
    except OSError as exc:
        logger.error("Could not read filled form at %s: %s", output_path, exc)
        return [_text_response("The filled form was created but could not be read for download. Please try again.")]

    if not file_bytes:
        return [_text_response("The filled form is empty. Please review your answers and try again.")]

    download_name = os.path.basename(output_path) or "filled_application_form.docx"
    download_type = content_type or "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    _clear_application_review_state(profile)
    save_profile(profile)

    return [
        _file_download_response(
            download_name,
            file_bytes,
            download_type,
            text="Approved! Your filled application form is ready — use the attachment below to download it.",
        )
    ]


def handle_cancel_application_collection(student_id: str) -> list:
    profile = get_profile(student_id)
    if profile:
        _clear_application_review_state(profile)
        save_profile(profile)
    return [_text_response("Application collection cancelled. Tap **Start Application** whenever you're ready to try again.")]


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
            if intent == "add_calendar_event":
                merged = _merge_timetable_data(
                    extracted_data,
                    _extract_calendar_fields_locally(text),
                )
                if _missing_calendar_fields(merged):
                    return _save_pending_calendar_add(profile, merged, agent_response)
                card = _calendar_add_confirmation_card(merged)
                return [_card_response("Please confirm this calendar event.", card)]
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

        if intent == "add_calendar_event":
            merged = _merge_timetable_data(
                extracted_data,
                _extract_calendar_fields_locally(text),
            )
            missing_calendar_fields = _missing_calendar_fields(merged)
            if missing_calendar_fields:
                return _save_pending_calendar_add(profile, merged, agent_response)
            card = _calendar_add_confirmation_card(merged)
            return [_card_response("Please confirm this calendar event.", card)]

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
        responses = [_text_response(
            "Profile updated! Running matching with your new details..."
        )]
        digest_result = handle_get_digest(student_id)
        if isinstance(digest_result, dict):
            responses.append(digest_result)
        else:
            responses.extend(digest_result)
        return responses
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

    if profile and profile.get("pending_action") == "complete_calendar_add" and not action.strip():
        return _handle_pending_calendar_add(profile, message.get("text") or "")

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

    if action == "start_event_registration":
        return handle_start_event_registration(value)

    if action == "confirm_event_registration":
        return handle_confirm_event_registration(student_id, value)

    if action == "cancel_event_registration":
        return [_text_response("Registration cancelled. No calendar changes were made.")]

    if action == "confirm_calendar_add":
        return handle_confirm_calendar_add(student_id, value)

    if action == "cancel_calendar_add":
        return [_text_response("No problem — I did not add anything to your calendar.")]

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

    if action == "skip_application_section":
        profile = get_profile(student_id)
        if profile and get_application_state(profile).get("step") == "collecting_list":
            state = get_application_state(profile)
            section_id = value.get("section_key") or _section_storage_id(state.get("current_gap") or {})
            if section_id:
                _mark_suggestion_reviewed(profile, section_id)
            return _skip_current_section(profile, state, "Skipped this section.")
        return [_text_response("No section is active to skip right now.")]

    if action == "accept_profile_suggestions":
        profile = get_profile(student_id)
        if profile and get_application_state(profile).get("step") == "collecting_list":
            state = get_application_state(profile)
            section_id = value.get("section_key") or _section_storage_id(state.get("current_gap") or {})
            return _accept_profile_suggestions(profile, state, section_id)
        return [_text_response("No section is waiting for profile suggestions right now.")]

    if action == "reject_profile_suggestions":
        profile = get_profile(student_id)
        if profile and get_application_state(profile).get("step") == "collecting_list":
            state = get_application_state(profile)
            section_id = value.get("section_key") or _section_storage_id(state.get("current_gap") or {})
            return _reject_profile_suggestions(profile, state, section_id)
        return [_text_response("No section is waiting for profile suggestions right now.")]

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
            user_token = get_graph_access_token(profile)
            if not user_token:
                return [_text_response("Please sign in with Microsoft to restore emails.")]
            success = restore_email(email_id, user_token)
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
    if _looks_like_calendar_add_message(text):
        return handle_profile_update(student_id, message.get("text") or text)

    if _looks_like_event_registration_message(text):
        return handle_event_registration(student_id, message.get("text") or text)

    if any(kw in text for kw in update_terms) or _looks_like_timetable_message(text):
        return handle_profile_update(student_id, message.get("text") or text)

    if "upload cv" in text or text == "cv":
        return [_text_response("Please attach your PDF CV to the chat first, then type CV again.")]

    event_commands = {
        "events", "show events", "competitions", "show me events", "event matches",
    }
    if text in event_commands:
        return handle_get_events(student_id)

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
        digest_terms = ["digest", "update", "what's new", "show me", "opportunities", "inbox"]
        help_terms = ["hello", "hi", "hey", "start", "help"]
        if not any(kw in text for kw in digest_terms + help_terms):
            return handle_text_pasted(student_id, message.get("text") or "")

    if any(kw in text for kw in ["digest", "update", "what's new", "show me", "opportunities", "inbox"]):
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
        "Did you mean to update your profile, add a class to your timetable, add an event to your calendar, or see your daily digest?"
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
