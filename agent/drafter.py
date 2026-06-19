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
from agent.form_filler import fill_application_form

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


def _fallback_extract_questions(raw_questions: str) -> list:
    questions = []
    seen = set()
    for line in (raw_questions or "").splitlines():
        text = line.strip().lstrip("-*0123456789. )\t")
        if not text:
            continue
        if "?" not in text and len(text.split()) < 4:
            continue
        key = text.lower()
        if key not in seen:
            seen.add(key)
            questions.append(text)
    return questions[:10]


def extract_application_questions(raw_questions: str) -> list:
    """
    Extract clean application questions from pasted form text.
    Returns a list of question strings.
    """
    raw_questions = (raw_questions or "").strip()
    if not raw_questions:
        return []

    prompt = f"""
You are helping a university scholarship assistant parse application forms.
Extract only the questions/prompts the student is expected to answer.

Return JSON only in this exact shape:
{{"questions": ["Question 1?", "Question 2?"]}}

Rules:
- Extract at most 10 questions.
- Keep each question concise but preserve its meaning.
- Ignore instructions, page headers, eligibility text, and upload requirements.
- If the text contains no answerable questions, return {{"questions": []}}.

Pasted application text:
{raw_questions}
"""

    try:
        response = openai_client.chat.completions.create(
            model=DEPLOYMENT,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=1200,
            temperature=0.1
        )
        raw = response.choices[0].message.content
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            values = parsed.get("questions", [])
        elif isinstance(parsed, list):
            values = parsed
        else:
            values = []

        questions = []
        seen = set()
        for value in values:
            text = str(value).strip()
            key = text.lower()
            if text and key not in seen:
                seen.add(key)
                questions.append(text)
        return questions[:10]

    except json.JSONDecodeError as e:
        logger.error(f"Question extraction JSON parse error: {e}")
        return _fallback_extract_questions(raw_questions)
    except Exception as e:
        logger.error(f"Question extraction error: {e}")
        return _fallback_extract_questions(raw_questions)


def generate_draft_answers(student_id: str, scholarship_id: str, questions: list[str]) -> dict:
    """
    Generate tailored answers for selected application questions.
    Returns {"answers": [{"question": "...", "answer": "..."}], "notes": "..."}.
    """
    profile = get_profile(student_id)
    scholarship = _get_scholarship(scholarship_id)

    if not profile:
        return {"error": f"Profile not found: {student_id}"}
    if not scholarship:
        return {"error": f"Scholarship not found: {scholarship_id}"}
    if not questions:
        return {"error": "No questions selected"}

    academic = profile.get("academic", {})
    prompt_profile = {
        "name": profile.get("name", ""),
        "faculty": academic.get("faculty", ""),
        "programme": academic.get("programme", ""),
        "year_of_study": academic.get("year_of_study", ""),
        "gpa": academic.get("gpa", ""),
        "interests": profile.get("interests", []),
        "activities": profile.get("activities", []),
        "cv_text": profile.get("cv_text", "")[:3500],
    }
    prompt_scholarship = {
        "id": scholarship.get("id"),
        "name": scholarship.get("name"),
        "provider": scholarship.get("provider"),
        "eligibility_raw": scholarship.get("eligibility_raw", ""),
        "application_method": scholarship.get("application_method", ""),
        "deadline_raw": scholarship.get("deadline_raw", ""),
    }

    prompt = f"""
You are drafting scholarship application responses for a university student.
Use the student profile, CV notes, and scholarship context to answer only the selected questions.

Rules:
- Be specific and credible; do not invent named awards, jobs, or experiences not present in the profile/CV.
- If evidence is missing, write a polished but honest answer and mention what detail the student should add.
- Keep each answer between 120 and 220 words unless the question clearly asks for a shorter response.
- Return JSON only in this exact shape:
{{"answers": [{{"question": "...", "answer": "..."}}], "notes": "..."}}

Student profile:
{json.dumps(prompt_profile, ensure_ascii=False, indent=2)}

Scholarship:
{json.dumps(prompt_scholarship, ensure_ascii=False, indent=2)}

Selected questions:
{json.dumps(questions, ensure_ascii=False, indent=2)}
"""

    try:
        response = openai_client.chat.completions.create(
            model=DEPLOYMENT,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=2500,
            temperature=0.35
        )
        parsed = json.loads(response.choices[0].message.content)
        answers = parsed.get("answers", []) if isinstance(parsed, dict) else []
        formatted = []
        for idx, item in enumerate(answers):
            if isinstance(item, dict):
                fallback_question = questions[idx] if idx < len(questions) else ""
                question = str(item.get("question") or fallback_question).strip()
                answer = str(item.get("answer") or "").strip()
            else:
                question = questions[idx] if idx < len(questions) else f"Question {idx + 1}"
                answer = str(item).strip()
            if question and answer:
                formatted.append({"question": question, "answer": answer})

        return {
            "scholarship_id": scholarship_id,
            "scholarship_name": scholarship.get("name", "Scholarship"),
            "answers": formatted,
            "notes": parsed.get("notes", "Review and personalize before submitting.") if isinstance(parsed, dict) else "Review and personalize before submitting."
        }
    except json.JSONDecodeError as e:
        logger.error(f"Draft answer JSON parse error: {e}")
        return {"error": "Failed to parse draft answers"}
    except Exception as e:
        logger.error(f"Draft answer generation error: {e}")
        return {"error": str(e)}


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

    mock_profile = {
        "id": "local_mock_student",
        "name": "Local Mock Student",
        "email": "local.mock.student@connect.hku.hk",
        "academic": {
            "faculty": "Engineering",
            "programme": "Bachelor of Engineering in Computer Science",
            "year_of_study": 2,
            "gpa": 3.7,
            "level": "undergraduate",
            "nationality": {
                "local_status": "local",
                "country_of_origin": "Hong Kong"
            },
        },
        "financial": {"financial_need_opt_in": False},
        "interests": ["AI", "robotics", "hackathons"],
        "activities": ["HKU Robotics Team", "Undergraduate research assistant"],
        "cv_text": "Engineering student with robotics, research, and hackathon experience.",
    }
    get_profile = lambda student_id: mock_profile

    scholarship_id = sys.argv[1] if len(sys.argv) > 1 else "ss_663"
    student_id     = sys.argv[2] if len(sys.argv) > 2 else mock_profile["id"]

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
