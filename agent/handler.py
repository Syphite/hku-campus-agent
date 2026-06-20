"""
agent/handler.py
Copilot Chat entry point for the HKU Campus Agent.
"""
import os
import json
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# Import agent modules using absolute paths from project root
from agent.profile  import get_profile, save_profile, build_profile_from_form, update_profile_fields, extract_cv_text
from agent.matching import run_matching
from agent.drafter  import extract_application_questions, generate_draft_answers
from agent.question_extractor import extract_questions_from_file
from agent.digest   import assemble_digest, format_digest_message

# Import event pipeline
from agent.events.event_extractor  import extract_events_for_student
from agent.conflict_checker        import run_conflict_checks_batch

# Import email pipeline
from agent.email_pipeline import run_inbox_pipeline

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

def _scholarship_cards(scholarships: list, tier: str) -> list:
    """Build a list of Adaptive Card attachments for scholarship results."""
    cards = []
    for s in scholarships:
        is_open   = s.get("is_open", False)
        strength  = s.get("match_strength", "partial")
        strength_emoji = "🟢" if strength == "strong" else "🟡"

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
        if is_open:
            actions.append({
                "type": "Action.Submit",
                "title": "Start Draft",
                "style": "positive",
                "data": {
                    "action": "start_draft",
                    "scholarship_id": s.get("scholarship_id") or s.get("id"),
                    "scholarship_name": s.get("name", "Scholarship")
                }
            })

        actions.append({
            "type": "Action.OpenUrl",
            "title": "View Scholarship",
            "url": s.get("application_url", "https://aas.hku.hk/apply-scholarships/")
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
    
    # Build and save profile
    profile = build_profile_from_form({
        "name":                  form_data.get("name", "Student"),
        "email":                 form_data.get("email", f"{student_id}@connect.hku.hk"),
        "faculty":               form_data.get("faculty", " "),
        "programme":             form_data.get("programme", " "),
        "year_of_study":         form_data.get("year_of_study", "1"),
        "gpa":                   form_data.get("gpa") or 0,
        "level":                 "postgraduate" if form_data.get("year_of_study") == "postgraduate" else "undergraduate",
        "local_status":          form_data.get("local_status", "local"),
        "country_of_origin":     form_data.get("country_of_origin", "Hong Kong"),
        "financial_need_opt_in": form_data.get("financial_need_opt_in") == "true",
        "interests":             [i.strip() for i in form_data.get("interests", " ").split(",") if i.strip()],
        "activities":            [form_data.get("activities", " ")],
        "notification_preference": form_data.get("notification_preference", "daily_morning"),
        "module_scholarships":   form_data.get("module_scholarships", "true"),
        "module_events":         form_data.get("module_events", "true"),
        "module_inbox":          form_data.get("module_inbox", "true"),
        "expected_graduation_year": 2028,
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

    # Inject timetable into profile before saving
    profile["timetable"] = {
        "blocked_slots": blocked_slots,
        "upcoming_deadlines": []
    }
    
    profile["id"] = student_id
    save_profile(profile)

    responses = [_text_response(
        f"Profile set up! Welcome, {profile.get('name', 'Student')}. "
        f"I've saved your timetable. Running your first digest now..."
    )]
    responses.extend(handle_get_digest(student_id))
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
        card = _load_card("onboarding_card")
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

    # Scholarship cards — apply now
    if digest["scholarships"]["apply_now"]:
        responses.append(_text_response("**📋 Scholarships — Apply Now:**"))
        for card in _scholarship_cards(digest["scholarships"]["apply_now"], "apply_now"):
            responses.append({
                "type": "message",
                "text": " ",
                "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]
            })

    # Scholarship cards — prepare
    if digest["scholarships"]["prepare"]:
        responses.append(_text_response("** Scholarships — Prepare Now:**"))
        for card in _scholarship_cards(digest["scholarships"]["prepare"][:3], "prepare"):
            responses.append({
                "type": "message",
                "text": " ",
                "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]
            })

    # Event cards — urgent
    if digest["events"]["urgent"]:
        responses.append(_text_response("**🏆 Events — Closing Soon:**"))
        for event in digest["events"]["urgent"][:3]:
            card = _event_card(event)
            responses.append({
                "type": "message",
                "text": " ",
                "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]
            })

    if inbox_summary and inbox_summary.get("archived_items"):
        review_item = inbox_summary["archived_items"][0]
        responses.append({
            "type": "message",
            "text": " ",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": _archive_review_card(review_item)
                }
            ]
        })

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
        card = _load_card("onboarding_card")
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
    action        = value.get("action", " ")

    logger.info(f"Message from {student_id}: type={activity_type} action={action} text={text[:50]}")

    # ─ Adaptive Card submissions ────────────────────────────────────────────
    if action == "onboarding_submit":
        return handle_onboarding_submit(student_id, value)

    if action == "semester_refresh_submit":
        return handle_semester_refresh(student_id, value)

    if action == "semester_refresh_dismiss":
        return handle_semester_refresh(student_id, value, dismissed=True)

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
    if "upload cv" in text:
        return [_text_response("Please attach your PDF CV to the chat first!")]

    profile = get_profile(student_id)
    if profile and profile.get("last_scholarship_id") and text:
        digest_terms = ["digest", "update", "what's new", "show me", "scholarships", "opportunities"]
        help_terms = ["hello", "hi", "hey", "start", "help"]
        if not any(kw in text for kw in digest_terms + help_terms):
            return handle_text_pasted(student_id, message.get("text") or "")

    if any(kw in text for kw in ["digest", "update", "what's new", "show me", "scholarships", "opportunities"]):
        return handle_get_digest(student_id)

    if any(kw in text for kw in ["draft", "apply", "application"]):
        return [_text_response(
            "Which scholarship would you like to draft? "
            "Tap 'Start Draft' on any scholarship card in your digest, "
            "or tell me the scholarship name."
        )]

    if any(kw in text for kw in ["hello", "hi", "hey", "start", "help"]):
        if not profile or not profile.get("onboarding_complete"):
            card = _load_card("onboarding_card")
            return [_card_response(
                "Hi! I'm your HKU Campus Agent. I find scholarships, competitions, "
                "and opportunities tailored to you, and help you apply. Let's get you set up:",
                card
            )]
        else:
            return handle_get_digest(student_id)

    # Default — return digest
    profile = get_profile(student_id)
    if not profile or not profile.get("onboarding_complete"):
        card = _load_card("onboarding_card")
        return [_card_response(
            "Hi! I'm your HKU Campus Agent. I find scholarships, competitions, "
            "and opportunities tailored to you, and help you apply. Let's get you set up:",
            card
        )]

    return handle_get_digest(student_id)

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
