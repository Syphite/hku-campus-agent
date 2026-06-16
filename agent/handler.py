"""
agent/handler.py

Copilot Chat entry point for the HKU Campus Agent.
Receives messages from M365 Copilot Chat, detects intent,
calls the right functions, and returns structured responses.

This is the file that gets registered with the M365 Agents SDK.
It handles five intents:
  1. onboarding_submit      — first time setup form submitted
  2. get_digest             — student wants their update
  3. start_draft            — student wants to draft an application
  4. undo_archive           — student wants to restore an archived email
  5. semester_refresh_submit — student updated their profile
  6. (default)              — general message, return digest or help
"""
import os
import sys
import json
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

# Ensure the project root is in sys.path so 'agent.' imports work universally
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

load_dotenv()
logger = logging.getLogger(__name__)

# Import agent modules using absolute paths from project root
from agent.profile  import get_profile, save_profile, build_profile_from_form, update_profile_fields
from agent.matching import run_matching
from agent.drafter  import draft_application
from agent.digest   import assemble_digest, format_digest_message

# Import event pipeline
from agent.events.event_extractor  import extract_events_for_student
from agent.conflict_checker        import run_conflict_checks_batch


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
    """
    Build a list of Adaptive Card attachments for scholarship results.
    One card per scholarship.
    """
    cards = []
    for s in scholarships:
        is_open   = s.get("is_open", False)
        tier_label = "Apply Now" if is_open else "Prepare"
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
                    {"title": "Match",    "value": strength.capitalize()},
                    {"title": "Deadline", "value": s.get("deadline_raw", "See scholarship page")},
                    {"title": "Reason",   "value": s.get("reason", "")},
                ]
            }
        ]

        if s.get("gap"):
            body.append({
                "type": "TextBlock",
                "text": f"⚠️ Gap: {s['gap']}",
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
                "text": f"📅 {s['calendar_note']}",
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
                    "scholarship_id": s.get("scholarship_id") or s.get("id")
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
            "version": "1.5",
            "body": body,
            "actions": actions
        }
        cards.append(card)
    return cards


def _event_card(event: dict) -> dict:
    """Build an Adaptive Card for a single event."""
    type_emoji = {
        "competition": "🏆", "hackathon": "💻", "scholarship": "📋",
        "internship": "💼", "workshop": "🛠️", "talk": "🎤",
        "cultural_exchange": "🌏", "volunteering": "🤝",
        "career_fair": "👔", "recruitment": "📢", "research": "🔬"
    }.get(event.get("type", "other"), "📌")

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
                {"title": "Type",      "value": event.get("type", "").replace("_"," ").capitalize()},
                {"title": "Organiser", "value": event.get("organiser", "")},
                {"title": "Deadline",  "value": event.get("deadline") or "See event page"},
                {"title": "Location",  "value": event.get("location", "")},
            ]
        },
        {
            "type": "TextBlock",
            "text": event.get("summary", ""),
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
        "version": "1.5",
        "body": body,
        "actions": actions
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
        "faculty":               form_data.get("faculty", ""),
        "programme":             form_data.get("programme", ""),
        "year_of_study":         form_data.get("year_of_study", "1"),
        "gpa":                   form_data.get("gpa") or 0,
        "level":                 "postgraduate" if form_data.get("year_of_study") == "postgraduate" else "undergraduate",
        "local_status":          form_data.get("local_status", "local"),
        "country_of_origin":     form_data.get("country_of_origin", "Hong Kong"),
        "financial_need_opt_in": form_data.get("financial_need_opt_in") == "true",
        "interests":             [i.strip() for i in form_data.get("interests", "").split(",") if i.strip()],
        "activities":            [form_data.get("activities", "")],
        "digest_frequency":      form_data.get("digest_frequency", "weekly"),
        "language_preference":   form_data.get("language_preference", "english"),
        "expected_graduation_year": 2028,
    })
    profile["id"] = student_id
    save_profile(profile)

    responses = [_text_response(
        f"Profile set up! Welcome, {profile.get('name', 'Student')}. "
        f"Running your first digest now..."
    )]
    responses.extend(handle_get_digest(student_id))
    return responses


def handle_get_digest(student_id: str) -> list:
    """Run full matching pipeline and return digest as Adaptive Cards."""
    profile = get_profile(student_id)
    if not profile:
        card = _load_card("onboarding_card")
        return [_card_response(
            "I don't have your profile yet. Let's get you set up:",
            card
        )]

    # Run scholarship matching
    scholarship_result = run_matching(student_id)

    # Run event extraction and conflict checking
    try:
        raw_events = extract_events_for_student(student_id)
        checked_events = run_conflict_checks_batch(raw_events, profile)
    except Exception as e:
        logger.error(f"Event pipeline error: {e}")
        checked_events = []

    # Assemble digest
    digest = assemble_digest(
        student_id=student_id,
        scholarship_result=scholarship_result,
        events=checked_events,
        inbox_summary=None  # teammate module connects here
    )

    responses = [_text_response(format_digest_message(digest))]

    # Scholarship cards — apply now
    if digest["scholarships"]["apply_now"]:
        responses.append(_text_response("**📋 Scholarships — Apply Now:**"))
        for card in _scholarship_cards(digest["scholarships"]["apply_now"], "apply_now"):
            responses.append({
                "type": "message",
                "text": "",
                "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]
            })

    # Scholarship cards — prepare
    if digest["scholarships"]["prepare"]:
        responses.append(_text_response("**📅 Scholarships — Prepare Now:**"))
        for card in _scholarship_cards(digest["scholarships"]["prepare"][:3], "prepare"):
            responses.append({
                "type": "message",
                "text": "",
                "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]
            })

    # Event cards — urgent
    if digest["events"]["urgent"]:
        responses.append(_text_response("**🏆 Events — Closing Soon:**"))
        for event in digest["events"]["urgent"][:3]:
            card = _event_card(event)
            responses.append({
                "type": "message",
                "text": "",
                "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]
            })

    return responses


def handle_start_draft(student_id: str, scholarship_id: str) -> list:
    """Draft a scholarship application and return as a message."""
    logger.info(f"Drafting {scholarship_id} for {student_id}")

    draft = draft_application(scholarship_id, student_id)

    if "error" in draft:
        return [_text_response(f"Sorry, I couldn't draft that application: {draft['error']}")]

    checklist = draft.get("checklist", {})
    missing   = checklist.get("missing", [])
    used      = checklist.get("used", [])
    strengthen = checklist.get("strengthen", [])

    card_body = [
        {
            "type": "TextBlock",
            "text": f"Draft: {draft.get('scholarship_name', '')}",
            "weight": "Bolder",
            "wrap": True
        },
        {
            "type": "TextBlock",
            "text": "**Cover Letter Draft:**",
            "weight": "Bolder",
            "spacing": "Medium"
        },
        {
            "type": "TextBlock",
            "text": draft.get("cover_letter", ""),
            "wrap": True,
            "size": "Small"
        },
        {
            "type": "TextBlock",
            "text": "**Application Notes:**",
            "weight": "Bolder",
            "spacing": "Medium"
        },
        {
            "type": "TextBlock",
            "text": draft.get("application_notes", ""),
            "wrap": True,
            "size": "Small"
        }
    ]

    if missing:
        card_body.append({
            "type": "TextBlock",
            "text": f"⚠️ **Still needed:** {', '.join(missing)}",
            "wrap": True,
            "color": "Warning",
            "size": "Small"
        })

    if strengthen:
        card_body.append({
            "type": "TextBlock",
            "text": f"💡 **Strengthen:** {'; '.join(strengthen)}",
            "wrap": True,
            "color": "Accent",
            "size": "Small"
        })

    card_body.append({
        "type": "TextBlock",
        "text": f"📍 **How to apply:** {draft.get('application_method', '')}",
        "wrap": True,
        "size": "Small",
        "spacing": "Medium"
    })

    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "body": card_body,
        "actions": [
            {
                "type": "Action.OpenUrl",
                "title": "Apply Now",
                "url": "https://hkuportal.hku.hk"
            }
        ]
    }

    return [_card_response(
        "Here's your draft. Review it, then apply via HKU Portal or the scholarship website.",
        card
    )]


def handle_semester_refresh(student_id: str, form_data: dict, dismissed: bool = False) -> list:
    """Handle semester refresh form submission."""
    if dismissed:
        return [_text_response("No problem — your profile is unchanged. I'll keep finding opportunities for you.")]

    updates = {}
    if form_data.get("gpa"):
        updates["academic"] = {"gpa": float(form_data["gpa"])}
    if form_data.get("year_of_study"):
        updates.setdefault("academic", {})["year_of_study"] = int(form_data["year_of_study"])

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

    Args:
        student_id: extracted from the M365 user token
        message: the incoming activity from Copilot Chat
                 {
                   "type": "message",
                   "text": "...",
                   "value": {...}  — present when an Adaptive Card is submitted
                 }

    Returns:
        List of response dicts to send back to Copilot Chat
    """
    activity_type = message.get("type", "message")
    text          = (message.get("text") or "").strip().lower()
    value         = message.get("value") or {}
    action        = value.get("action", "")

    logger.info(f"Message from {student_id}: type={activity_type} action={action} text={text[:50]}")

    # ── Adaptive Card submissions ────────────────────────────────────────────
    if action == "onboarding_submit":
        return handle_onboarding_submit(student_id, value)

    if action == "semester_refresh_submit":
        return handle_semester_refresh(student_id, value)

    if action == "semester_refresh_dismiss":
        return handle_semester_refresh(student_id, value, dismissed=True)

    if action == "start_draft":
        scholarship_id = value.get("scholarship_id", "")
        return handle_start_draft(student_id, scholarship_id)

    # ── Text messages ────────────────────────────────────────────────────────
    if any(kw in text for kw in ["digest", "update", "what's new", "show me", "scholarships", "opportunities"]):
        return handle_get_digest(student_id)

    if any(kw in text for kw in ["draft", "apply", "application"]):
        return [_text_response(
            "Which scholarship would you like to draft? "
            "Tap 'Start Draft' on any scholarship card in your digest, "
            "or tell me the scholarship name."
        )]

    if any(kw in text for kw in ["hello", "hi", "hey", "start", "help"]):
        profile = get_profile(student_id)
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
            "Hi! Let me set up your profile first:", card
        )]

    return handle_get_digest(student_id)


# ---------------------------------------------------------------------------
# Local test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    student_id = sys.argv[1] if len(sys.argv) > 1 else "persona_alex_chen"

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
