"""Helpers for profile application_state persistence."""

from copy import deepcopy
from typing import Any


def get_application_state(profile: dict) -> dict:
    state = profile.get("application_state")
    return deepcopy(state) if isinstance(state, dict) else {}


def set_application_state(profile: dict, state: dict) -> None:
    profile["application_state"] = state


def clear_application_state(profile: dict) -> None:
    profile.pop("application_state", None)


def init_application_state(
    profile: dict,
    *,
    scholarship_id: str,
    input_path: str,
    output_path: str,
    content_type: str,
    schema: dict,
    filled_data: dict,
    step: str = "collecting_list",
) -> dict:
    state = {
        "scholarship_id": scholarship_id,
        "step": step,
        "input_path": input_path,
        "output_path": output_path,
        "content_type": content_type,
        "schema": schema,
        "filled_data": filled_data,
        "form_plan": {},
        "pending_list_data": {},
        "pending_free_fields": {},
        "long_text_drafts": {},
        "skipped_sections": [],
        "ai_drafted_keys": [],
        "profile_suggestions": {},
        "suggestions_reviewed": [],
        "gap_queue": [],
        "current_gap": None,
        "form_json": None,
    }
    set_application_state(profile, state)
    return state


def update_application_state(profile: dict, **updates: Any) -> dict:
    state = get_application_state(profile)
    state.update(updates)
    set_application_state(profile, state)
    return state
