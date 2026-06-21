"""
agent/profile.py
Reads and writes student profiles from Cosmos DB.
No LLM involved — pure data operations.
"""

import os
import io
import logging
from datetime import datetime, timezone
from typing import Optional

from azure.cosmos import CosmosClient, exceptions
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

cosmos     = CosmosClient(os.environ["COSMOS_ENDPOINT"], os.environ["COSMOS_KEY"])
db         = cosmos.get_database_client(os.environ["COSMOS_DATABASE"])
container  = db.get_container_client("profiles")


# ---------------------------------------------------------------------------
# Read / Write
# ---------------------------------------------------------------------------

def get_profile(student_id: str) -> Optional[dict]:
    """Fetch a student profile from Cosmos DB. Returns None if not found."""
    try:
        return container.read_item(item=student_id, partition_key=student_id)
    except exceptions.CosmosResourceNotFoundError:
        return None
    except Exception as e:
        logger.error(f"Error reading profile {student_id}: {e}")
        return None


def save_profile(profile: dict) -> bool:
    """Upsert a full student profile into Cosmos DB."""
    try:
        profile["last_updated"] = datetime.now(timezone.utc).isoformat()
        container.upsert_item(profile)
        logger.info(f"Profile saved: {profile['id']}")
        return True
    except Exception as e:
        logger.error(f"Error saving profile {profile.get('id')}: {e}")
        return False


def update_profile_fields(student_id: str, updates: dict) -> bool:
    """
    Update specific fields in an existing profile.
    Used for semester refresh (GPA update, year increment etc.)
    """
    profile = get_profile(student_id)
    if not profile:
        logger.error(f"Profile not found: {student_id}")
        return False

    def deep_update(base: dict, updates: dict):
        for key, value in updates.items():
            if isinstance(value, dict) and key in base and isinstance(base[key], dict):
                deep_update(base[key], value)
            else:
                base[key] = value

    deep_update(profile, updates)
    return save_profile(profile)


def get_graph_access_token(profile: dict | None) -> str | None:
    """Return delegated Graph access token saved after OAuth sign-in."""
    if not profile:
        return None
    token = profile.get("graph_token")
    if isinstance(token, dict):
        value = token.get("token")
        return str(value).strip() if value else None
    if token:
        return str(token).strip()
    return None


def save_graph_token(student_id: str, token_payload) -> bool:
    """Persist delegated Graph token from Bot Framework OAuth sign-in."""
    profile = get_profile(student_id)
    if not profile:
        logger.error("Cannot save graph token — profile not found: %s", student_id)
        return False

    access_token = ""
    expiration = None
    if isinstance(token_payload, str):
        access_token = token_payload.strip()
    elif isinstance(token_payload, dict):
        access_token = str(token_payload.get("token") or "").strip()
        expiration = token_payload.get("expiration")

    if not access_token:
        logger.error("OAuth token response missing access token for %s", student_id)
        return False

    profile["graph_token"] = access_token
    if expiration:
        profile["graph_token_expires_at"] = expiration
    profile["graph_token_saved_at"] = datetime.now(timezone.utc).isoformat()
    return save_profile(profile)


def clear_pending_graph_command(student_id: str) -> str | None:
    profile = get_profile(student_id)
    if not profile:
        return None
    pending = profile.pop("pending_graph_command", None)
    if pending:
        save_profile(profile)
    return pending


# ---------------------------------------------------------------------------
# CV extraction
# ---------------------------------------------------------------------------

def extract_cv_text(file_bytes: bytes, filename: str) -> str:
    """
    Extract plain text from an uploaded CV file.
    Supports .pdf and .docx. No LLM involved.
    Returns empty string on failure.
    """
    filename_lower = filename.lower()

    if filename_lower.endswith(".pdf"):
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                return "\n".join(
                    page.extract_text() or "" for page in pdf.pages
                ).strip()
        except Exception as e:
            logger.error(f"PDF extraction failed: {e}")
            return ""

    elif filename_lower.endswith(".docx"):
        try:
            from docx import Document
            doc = Document(io.BytesIO(file_bytes))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip()).strip()
        except Exception as e:
            logger.error(f"DOCX extraction failed: {e}")
            return ""

    else:
        logger.warning(f"Unsupported file type: {filename}")
        return ""


# ---------------------------------------------------------------------------
# Profile builder (used during onboarding form submission)
# ---------------------------------------------------------------------------

def build_profile_from_form(form_data: dict, cv_bytes: bytes = None, cv_filename: str = None) -> dict:
    """
    Builds a profile dict from onboarding form data.
    form_data keys match the onboarding form fields.
    """
    student_id = f"student_{form_data['email'].replace('@', '').replace('.', '')}"
    cv_text = ""
    if cv_bytes and cv_filename:
        cv_text = extract_cv_text(cv_bytes, cv_filename)

    raw_year = str(form_data.get("year_of_study", "1"))
    year_val = raw_year if raw_year == "postgraduate" else int(raw_year)
    notification_preference = form_data.get("notification_preference", "daily_morning")
    digest_frequency = {
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
    }.get(notification_preference, {
        "email": "daily",
        "scholarships": "weekly",
        "events": "daily"
    })

    # NEW: Parse Timetable from the 3 rows
    blocked_slots = []
    for i in range(1, 4):
        code  = form_data.get(f"class{i}_code", "").strip()
        day   = form_data.get(f"class{i}_day", "").strip()
        start = form_data.get(f"class{i}_start", "").strip()
        end   = form_data.get(f"class{i}_end", "").strip()
        
        # Only add if all fields for the row are filled
        if code and day and start and end:
            blocked_slots.append({
                "day": day,
                "start": start,
                "end": end,
                "label": code
            })

    return {
        "id":                  student_id,
        "name":                form_data.get("name", ""),
        "email":               form_data.get("email", ""),
        "academic": {
            "faculty":                 form_data.get("faculty", ""),
            "programme":               form_data.get("programme", ""),
            "year_of_study":           year_val,
            "gpa":                     float(form_data.get("gpa", 0.0)),
            "level":                   form_data.get("level", "undergraduate"),
            "nationality": {
                "local_status":    form_data.get("local_status", "local"),
                "country_of_origin": form_data.get("country_of_origin", "Hong Kong")
            },
            "expected_graduation_year": int(form_data.get("expected_graduation_year", 2028)),
        },
        "financial": {
            "financial_need_opt_in": form_data.get("financial_need_opt_in", False)
        },
        "interests":        form_data.get("interests", []),
        "activities":       form_data.get("activities", []),
        "cv_text":          "",
        "processed_email_ids": [],
        "preferences": {
            "modules_enabled": [
                module
                for module, enabled in [
                    ("scholarships", form_data.get("module_scholarships", "true") == "true"),
                    ("events", form_data.get("module_events", "true") == "true"),
                    ("inbox", form_data.get("module_inbox", "true") == "true")
                ]
                if enabled
            ],
            "digest_frequency": digest_frequency
        },
        
        # UPDATED: Use the parsed timetable
        "timetable": {
            "blocked_slots":       blocked_slots,
            "upcoming_deadlines":  []
        },
        "consent": {
            "inbox": str(form_data.get("consent_inbox", "false")).lower() == "true",
            "calendar": str(form_data.get("consent_calendar", "false")).lower() == "true",
        },
        "onboarding_complete": True,
        "last_updated":        datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Local test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    mock_profile = {
        "id": "local_mock_student",
        "name": "Local Mock Student",
        "email": "local.mock.student@connect.hku.hk",
        "academic": {
            "faculty":                 "Engineering",
            "programme":               "Bachelor of Engineering in Computer Science",
            "year_of_study":           2,
            "gpa":                     3.7,
            "level":                   "undergraduate",
            "nationality": {
                "local_status":      "local",
                "country_of_origin": "Hong Kong"
            },
            "expected_graduation_year": 2028,
        },
        "financial":       {"financial_need_opt_in": False},
        "interests":       ["AI", "robotics", "hackathons"],
        "activities":      ["HKU Robotics Team", "Undergraduate research assistant", "Volunteer at Code4HK"],
        "cv_text":         "Local Mock Student — Engineering (Computer Science) Year 2. GPA 3.7. HKU Robotics Team. Hackathon participant.",
        "processed_email_ids": [],
        "preferences": {
            "modules_enabled": ["scholarships", "events", "inbox"],
            "digest_frequency": {
                "email": "daily",
                "scholarships": "weekly",
                "events": "daily"
            }
        },
        "timetable": {
            "blocked_slots": [
                {"day": "Monday", "start": "10:00", "end": "12:00", "label": "COMP3230"}
            ],
            "upcoming_deadlines": []
        },
        "onboarding_complete": True,
    }

    print("Saving local mock profile...")
    success = save_profile(mock_profile)
    print(f"Save: {'OK' if success else 'FAILED'}")

    print("\nReading back...")
    loaded = get_profile(mock_profile["id"])
    if loaded:
        print(f"  Name: {loaded['name']}")
        print(f"  Faculty: {loaded['academic']['faculty']}")
        print(f"  GPA: {loaded['academic']['gpa']}")
        print("  OK")
    else:
        print("  FAILED — profile not found")
