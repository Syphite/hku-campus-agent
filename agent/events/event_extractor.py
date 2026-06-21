"""
agent/events/event_extractor.py

Takes raw post/email text from any source (LinkedIn, Xiaohongshu, email,
competition websites) and extracts structured event data using GPT-4o.

Processes all sources identically — source type is just metadata.
Called after email classification (relevant emails only) and after
social media mock/real scraping.
"""

import json
import logging
import os
from datetime import datetime, timezone

from agent.event_registration import normalize_event_calendar_fields
from agent.events.event_filters import filter_open_events

from dotenv import load_dotenv
from openai import AzureOpenAI

load_dotenv()
logger = logging.getLogger(__name__)

openai_client = AzureOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version="2024-12-01-preview"
)
DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

EXTRACT_BATCH_SIZE = 4

EXTRACT_PROMPT = """
You are an event extraction assistant for HKU students.
Extract structured event information from the provided text (which may be from an email, LinkedIn, or Xiaohongshu).

For each item, extract the following fields. If a field is not mentioned, use null or an empty list.
Keep eligibility and summary under 120 characters each.

- is_relevant: true only if this is a genuine student event or opportunity such as a competition, hackathon, internship with a specific application deadline, workshop, talk, cultural programme, volunteering opportunity, career fair, recruitment event, or research opportunity. false if it is noise (food reviews, generic job ads for non-students, unrelated content).
- Set is_relevant: false for posts primarily about scholarship applications. Scholarship matching comes from the Azure AI Search scholarship index, not social media scraping.
- Set is_relevant: false for events whose application deadline or scheduled end date is already in the past.
- type: "competition" | "hackathon" | "internship" | "workshop" | "talk" | "cultural_exchange" | "volunteering" | "career_fair" | "recruitment" | "research" | "other"
- title: clean, concise event name
- organiser: who is running it
- deadline: application/registration deadline as ISO date YYYY-MM-DD (or null)
- event_sessions: list of session times if mentioned. Use either:
  * recurring weekday sessions: {{"day": "Tuesday", "start": "14:00", "end": "17:00", "label": "Team sessions"}}
  * fixed calendar dates: {{"date": "2026-06-24", "end_date": "2026-06-25", "start": "19:00", "end": "19:00", "label": "Finals"}}
  For multi-day events, set date to the start day and end_date to the end day with clock times.
- eligibility: plain English description of who can apply (max 120 chars)
- faculty_relevant: list of relevant faculties (e.g., ["Engineering", "all"])
- year_relevant: list of eligible years (e.g., ["1", "2", "3", "4", "all"])
- skills_required: list of skills or []
- benefits: list of prizes, stipends, certificates, or networking benefits
- location: "online" | city/venue name | "hybrid"
- language: "english" | "mandarin" | "cantonese" | "bilingual"
- summary: one sentence in English summarising the opportunity (max 120 chars)
- source_id: the exact "id" field from the input item

Return ONLY a JSON object with key "events" containing an array of extracted items.
No preamble, no markdown fences.

Example output:
{{
  "events": [
    {{
      "source_id": "li_001",
      "is_relevant": true,
      "type": "hackathon",
      "title": "Microsoft Imagine Cup 2026",
      "organiser": "Microsoft Hong Kong",
      "deadline": "2026-07-15",
      "event_sessions": [],
      "eligibility": "All HK university students, teams of 1-4",
      "faculty_relevant": ["all"],
      "year_relevant": ["all"],
      "skills_required": ["AI/ML", "cloud computing"],
      "benefits": ["HK$50,000 prize", "trip to global finals"],
      "location": "Hong Kong",
      "language": "english",
      "summary": "Microsoft student innovation competition; deadline July 15."
    }}
  ]
}}

Items to process:
{items}
"""


def _strip_json_fences(raw: str) -> str:
    text = (raw or "").strip()
    if "```" in text:
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def _parse_event_payload(raw: str) -> list:
    """Parse LLM JSON; salvage complete objects from truncated arrays if needed."""
    text = _strip_json_fences(raw)
    if not text:
        return []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        salvaged = _salvage_json_objects(text)
        if salvaged:
            logger.warning("Salvaged %s complete event object(s) from truncated JSON", len(salvaged))
            return salvaged
        raise

    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for value in parsed.values():
            if isinstance(value, list):
                return value
    return []


def _salvage_json_objects(text: str) -> list:
    """Extract complete top-level JSON objects from a truncated array response."""
    objects = []
    depth = 0
    start = None
    in_string = False
    escape = False

    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue

        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    chunk = text[start:index + 1]
                    try:
                        item = json.loads(chunk)
                        if isinstance(item, dict):
                            objects.append(item)
                    except json.JSONDecodeError:
                        pass
                    start = None

    return objects


def _compact_items(raw_items: list) -> list:
    return [
        {
            "id": item.get("id"),
            "source": item.get("source"),
            "raw_text": item.get("raw_text", "")[:900],
            "date": item.get("posted_date") or item.get("received_date", ""),
            "from": item.get("poster") or item.get("sender", ""),
        }
        for item in raw_items
    ]


def _extract_events_batch(batch: list) -> list:
    if not batch:
        return []

    prompt = EXTRACT_PROMPT.format(
        items=json.dumps(_compact_items(batch), indent=2, ensure_ascii=False)
    )

    response = openai_client.chat.completions.create(
        model=DEPLOYMENT,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=3500,
        temperature=0.1,
    )
    choice = response.choices[0]
    raw = choice.message.content or "{}"
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason == "length":
        logger.warning("Event extraction batch hit token limit; attempting salvage")

    return _parse_event_payload(raw)


def _enrich_events(extracted: list, source_map: dict) -> list:
    results = []
    for event in extracted:
        if not isinstance(event, dict):
            continue
        if not event.get("is_relevant", True):
            continue
        sid = event.get("source_id")
        if sid and sid in source_map:
            orig = source_map[sid]
            event["source"] = orig.get("source")
            event["source_url"] = orig.get("source_url", "")
            event["found_for"] = orig.get("found_for", "all")
            event["posted_date"] = orig.get("posted_date") or orig.get("received_date", "")
            if not event.get("year_relevant"):
                event["year_relevant"] = orig.get("year_tags") or ["all"]
            event["_source_text"] = orig.get("raw_text", "")
        event["extracted_at"] = datetime.now(timezone.utc).isoformat()
        results.append(normalize_event_calendar_fields(event))
    return results


def extract_events(raw_items: list) -> list:
    """
    Extract structured events from raw posts or emails.

    Args:
        raw_items: list of dicts, each with at minimum:
            - id: unique identifier
            - raw_text: the post or email body
            - source: "linkedin" | "xiaohongshu" | "email" | "website"
            - posted_date or received_date (optional)
            - poster or sender (optional)

    Returns:
        List of structured event dicts (relevant only, irrelevant filtered out)
    """
    if not raw_items:
        return []

    source_map = {item["id"]: item for item in raw_items if item.get("id")}
    all_extracted = []

    for offset in range(0, len(raw_items), EXTRACT_BATCH_SIZE):
        batch = raw_items[offset:offset + EXTRACT_BATCH_SIZE]
        try:
            batch_results = _extract_events_batch(batch)
            all_extracted.extend(batch_results)
        except json.JSONDecodeError as exc:
            logger.error("Event extraction JSON error for batch %s-%s: %s", offset, offset + len(batch), exc)
        except Exception as exc:
            logger.error("Event extraction error for batch %s-%s: %s", offset, offset + len(batch), exc)

    results = _enrich_events(all_extracted, source_map)
    open_results = filter_open_events(results)
    logger.info(
        "Extracted %s relevant events from %s items (%s still open)",
        len(open_results),
        len(raw_items),
        len(open_results),
    )
    return open_results


def extract_events_for_student(student_id: str) -> list:
    """
    Load mock social posts and return personalized event matches for the student.
    Stage 1 keyword filter → extract → Stage 2 LLM reasoning.
    """
    from agent.events.event_matching import run_event_matching

    return run_event_matching(student_id)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    mock_profile = {
        "id": "local_mock_student",
        "name": "Local Mock Student",
        "interests": ["AI", "robotics", "hackathons"],
        "academic": {
            "faculty": "Engineering",
            "programme": "Bachelor of Engineering in Computer Science",
            "year_of_study": 2,
        }
    }
    student_id = sys.argv[1] if len(sys.argv) > 1 else mock_profile["id"]
    print(f"\nExtracting events for {student_id}...")

    events = extract_events_for_student(student_id)
    print(f"\nFound {len(events)} relevant events:\n")
    for e in events:
        print(f"  [{e.get('type','?')}] {e.get('title','?')}")
        print(f"    Organiser : {e.get('organiser','?')}")
        print(f"    Deadline  : {e.get('deadline','N/A')}")
        print(f"    Sessions  : {e.get('event_sessions',[])}")
        print(f"    Eligible  : {e.get('eligibility','?')}")
        print(f"    Summary   : {e.get('summary','')}")
        print()
