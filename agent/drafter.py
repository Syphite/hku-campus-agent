"""
agent/drafter.py
Drafts scholarship applications using student profile + scholarship details.
Called when a student taps "Start Draft" on a matched scholarship.
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from openai import AzureOpenAI
from dotenv import load_dotenv

from agent.profile import get_profile

load_dotenv()
logger = logging.getLogger(__name__)

SEARCH_ENDPOINT = os.environ["AZURE_SEARCH_ENDPOINT"]
SEARCH_API_KEY  = os.environ["AZURE_SEARCH_API_KEY"]
INDEX_NAME      = os.environ.get("SCHOLARSHIP_INDEX_NAME", "scholarships")

openai_client = AzureOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version="2024-12-01-preview"
)
DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "application_draft.txt")
with open(PROMPT_PATH) as f:
    DRAFT_PROMPT = f.read()


def _get_scholarship(scholarship_id: str) -> dict:
    client = SearchClient(SEARCH_ENDPOINT, INDEX_NAME, AzureKeyCredential(SEARCH_API_KEY))
    try:
        return client.get_document(key=scholarship_id)
    except Exception as e:
        logger.error(f"Error fetching {scholarship_id}: {e}")
        return {}


def draft_application(scholarship_id: str, student_id: str, form_path: Optional[str] = None) -> dict:
    """
    Generate a complete application draft for a specific scholarship.
    If a form_path is provided, it will also attempt to fill the PDF/DOCX form.
    """
    profile     = get_profile(student_id)
    scholarship = _get_scholarship(scholarship_id)

    if not profile:
        return {"error": f"Profile not found: {student_id}"}
    if not scholarship:
        return {"error": f"Scholarship not found: {scholarship_id}"}

    academic  = profile.get("academic", {})
    financial = profile.get("financial", {})

    profile_for_prompt = {
        "name":             profile.get("name", ""),
        "email":            profile.get("email", ""),
        "faculty":          academic.get("faculty", ""),
        "programme":        academic.get("programme", ""),
        "year_of_study":    academic.get("year_of_study", ""),
        "gpa":              academic.get("gpa", ""),
        "level":            academic.get("level", ""),
        "nationality":      academic.get("nationality", {}),
        "financial_need":   financial.get("financial_need_opt_in", False),
        "interests":        profile.get("interests", []),
        "activities":       profile.get("activities", []),
        "cv_text":          profile.get("cv_text", "")[:2000],
    }

    scholarship_for_prompt = {
        "id":                 scholarship.get("id"),
        "name":               scholarship.get("name"),
        "provider":           scholarship.get("provider"),
        "amount":             f"{scholarship.get('amount', '')} {scholarship.get('currency', 'HKD')}",
        "eligibility_raw":    scholarship.get("eligibility_raw", ""),
        "submission_materials": scholarship.get("submission_materials", []),
        "application_method": scholarship.get("application_method", ""),
        "application_url":    scholarship.get("application_url", ""),
        "deadline_raw":       scholarship.get("deadline_raw", ""),
        "is_open":            scholarship.get("is_open", False),
    }

    prompt = DRAFT_PROMPT.format(
        student_profile=json.dumps(profile_for_prompt, indent=2, ensure_ascii=False),
        scholarship=json.dumps(scholarship_for_prompt, indent=2, ensure_ascii=False)
    )

    try:
        response = openai_client.chat.completions.create(
            model=DEPLOYMENT,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=2000,
            temperature=0.3
        )
        raw    = response.choices[0].message.content
        draft  = json.loads(raw)

        draft["scholarship_id"] = scholarship_id
        draft["student_id"]     = student_id
        draft["drafted_at"]     = datetime.now(timezone.utc).isoformat()
        draft["status"]         = "pending_review"

        # --- NEW: Form Filling Logic ---
        if form_path and os.path.exists(form_path):
            filename = os.path.basename(form_path)
            output_filename = f"filled_{filename}"
            output_path = os.path.join(os.path.dirname(form_path), output_filename)
            
            # Inject the drafted cover letter so the DOCX filler can use it
            scholarship["drafted_cover_letter"] = draft.get("cover_letter", "")
            
            success = fill_application_form(form_path, output_path, profile, scholarship)
            if success:
                draft["filled_form_path"] = output_path
                draft["application_notes"] += "\n\n✅ I have also pre-filled the application form for you. Please review it before submitting."
            else:
                draft["application_notes"] += "\n\n⚠️ I attempted to fill the application form but encountered an error. Please fill it manually."

        logger.info(f"Draft generated for {scholarship_id} / {student_id}")
        return draft

    except json.JSONDecodeError as e:
        logger.error(f"Draft JSON parse error: {e}")
        return {"error": "Failed to parse draft response"}
    except Exception as e:
        logger.error(f"Draft generation error: {e}")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Local test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    scholarship_id = sys.argv[1] if len(sys.argv) > 1 else "ss_663"
    student_id     = sys.argv[2] if len(sys.argv) > 2 else "persona_alex_chen"

    print(f"\nDrafting application for {scholarship_id} / {student_id}...")
    draft = draft_application(scholarship_id, student_id)

    if "error" in draft:
        print(f"ERROR: {draft['error']}")
    else:
        print(f"\nScholarship: {draft.get('scholarship_name')}")
        print(f"Status: {draft.get('status')}")
        print(f"\nCOVER LETTER:\n{draft.get('cover_letter', '')[:500]}...")
        print(f"\nFORM FIELDS:")
        for k, v in draft.get("form_fields", {}).items():
            if v: print(f"  {k}: {v}")
        print(f"\nCHECKLIST:")
        print(f"  Used:      {draft.get('checklist', {}).get('used', [])}")
        print(f"  Missing:   {draft.get('checklist', {}).get('missing', [])}")
        print(f"  Strengthen:{draft.get('checklist', {}).get('strengthen', [])}")
        print(f"\nNotes: {draft.get('application_notes', '')}")
        print(f"\nMethod: {draft.get('application_method', '')}")
