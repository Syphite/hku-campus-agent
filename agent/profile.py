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
    student_id = f"student_{form_data['email'].replace('@', '_').replace('.', '_')}"

    cv_text = ""
    if cv_bytes and cv_filename:
        cv_text = extract_cv_text(cv_bytes, cv_filename)

    return {
        "id":                  student_id,
        "name":                form_data.get("name", ""),
        "email":               form_data.get("email", ""),
        "language_preference": form_data.get("language_preference", "english"),
        "academic": {
            "faculty":                 form_data.get("faculty", ""),
            "programme":               form_data.get("programme", ""),
            "year_of_study":           int(form_data.get("year_of_study", 1)),
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
        "cv_text":          cv_text,
        "timetable": {
            "blocked_slots":       [],
            "upcoming_deadlines":  []
        },
        "digest_frequency":    form_data.get("digest_frequency", "weekly"),
        "onboarding_complete": True,
        "last_updated":        datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Local test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)

    # Load Alex persona and save as a real profile
    with open("tests/personas/persona_alex_chen.json") as f:
        persona = json.load(f)

    # Convert persona format to profile format
    profile = {
        "id": persona["id"],
        "name": persona["profile"]["name"],
        "email": "alex.chen@connect.hku.hk",
        "language_preference": persona["profile"]["language_preference"],
        "academic": {
            "faculty":                 persona["academic"]["faculty"],
            "programme":               persona["academic"]["programme"],
            "year_of_study":           persona["academic"]["year_of_study"],
            "gpa":                     persona["academic"]["gpa"],
            "level":                   persona["academic"]["level"],
            "nationality": {
                "local_status":      persona["academic"]["nationality"],
                "country_of_origin": "Hong Kong"
            },
            "expected_graduation_year": persona["academic"]["expected_graduation_year"],
        },
        "financial":       persona["financial"],
        "interests":       persona["interests"],
        "activities":      ["HKU Robotics Team", "Undergraduate research assistant", "Volunteer at Code4HK"],
        "cv_text":         "Alex Chen — Engineering (Computer Science) Year 2. GPA 3.7. HKU Robotics Team. Hackathon participant.",
        "timetable":       persona["timetable"],
        "digest_frequency": "weekly",
        "onboarding_complete": True,
    }

    print("Saving Alex Chen profile...")
    success = save_profile(profile)
    print(f"Save: {'OK' if success else 'FAILED'}")

    print("\nReading back...")
    loaded = get_profile(persona["id"])
    if loaded:
        print(f"  Name: {loaded['name']}")
        print(f"  Faculty: {loaded['academic']['faculty']}")
        print(f"  GPA: {loaded['academic']['gpa']}")
        print("  OK")
    else:
        print("  FAILED — profile not found")
