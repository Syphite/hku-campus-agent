"""
agent/events/event_extractor.py

Takes raw post/email text from any source (LinkedIn, Xiaohongshu, email,
competition websites) and extracts structured event data using GPT-4o.

Processes all sources identically — source type is just metadata.
Called after email classification (relevant emails only) and after
social media mock/real scraping.
"""

import os
import json
import logging
from datetime import datetime, timezone
from openai import AzureOpenAI
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

openai_client = AzureOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version="2024-12-01-preview"
)
DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

EXTRACT_PROMPT = """
You are an event extraction assistant for HKU students. 
Extract structured event information from the provided text (which may be from an email, LinkedIn, or Xiaohongshu).

For each item, extract the following fields. If a field is not mentioned, use null or an empty list.

- is_relevant: true if this is a genuine student opportunity (competition, hackathon, scholarship, internship, talk, cultural programme, volunteering). false if it is noise (food reviews, generic job ads for non-students, unrelated content).
- type: "competition" | "hackathon" | "scholarship" | "internship" | "workshop" | "talk" | "cultural_exchange" | "volunteering" | "career_fair" | "recruitment" | "research" | "other"
- title: clean, concise event name
- organiser: who is running it
- deadline: application/registration deadline as ISO date YYYY-MM-DD (or null)
- event_sessions: list of recurring session times if mentioned (e.g., [{"day": "Tuesday", "start": "14:", "end": "17:00", "label": "Team sessions"}])
- eligibility: plain English description of who can apply
- faculty_relevant: list of relevant faculties (e.g., ["Engineering", "all"])
- year_relevant: list of eligible years (e.g., ["1", "2", "3", "4", "all"])
- skills_required: list of skills or []
- benefits: list of prizes, stipends, certificates, or networking benefits
- location: "online" | city/venue name | "hybrid"
- language: "english" | "mandarin" | "cantonese" | "bilingual"
- summary: one sentence in English summarising the opportunity and why it matters
- source_id: the exact "id" field from the input item

Return ONLY a valid JSON array. No preamble, no markdown fences.

Example output:
[
  {{
    "source_id": "li_001",
    "is_relevant": true,
    "type": "hackathon",
    "title": "Microsoft Imagine Cup 2026",
    "organiser": "Microsoft Hong Kong",
    "deadline": "2026-07-15",
    "event_sessions": [],
    "eligibility": "All university students in Hong Kong, team of 1-4",
    "faculty_relevant": ["all"],
    "year_relevant": ["all"],
    "skills_required": ["AI/ML", "cloud computing"],
    "benefits": ["HK$50,000 prize", "trip to global finals"],
    "location": "Hong Kong",
    "language": "english",
    "summary": "Microsoft global student innovation competition open to all HK university students, deadline July 15."
  }}
]

Items to process:
{items}
"""


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

    # Build compact input — cap text to save tokens
    compact = [
        {
            "id":       item.get("id"),
            "source":   item.get("source"),
            "raw_text": item.get("raw_text", "")[:1200],
            "date":     item.get("posted_date") or item.get("received_date", ""),
            "from":     item.get("poster") or item.get("sender", ""),
        }
        for item in raw_items
    ]

    prompt = EXTRACT_PROMPT.format(
        items=json.dumps(compact, indent=2, ensure_ascii=False)
    )

    try:
        response = openai_client.chat.completions.create(
            model=DEPLOYMENT,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
            temperature=0.1
        )
        raw = response.choices[0].message.content.strip()

        # Strip markdown fences if present
        if "```" in raw:
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        extracted = json.loads(raw)
        if isinstance(extracted, dict):
            # GPT wrapped in object — extract the list
            extracted = next(
                (v for v in extracted.values() if isinstance(v, list)), []
            )

        # Build source lookup for enrichment
        source_map = {item["id"]: item for item in raw_items}

        results = []
        for event in extracted:
            if not event.get("is_relevant", True):
                continue
            sid = event.get("source_id")
            if sid and sid in source_map:
                orig = source_map[sid]
                event["source"]       = orig.get("source")
                event["source_url"]   = orig.get("source_url", "")
                event["found_for"]    = orig.get("found_for", "all")
                event["posted_date"]  = orig.get("posted_date") or orig.get("received_date", "")
            event["extracted_at"] = datetime.now(timezone.utc).isoformat()
            results.append(event)

        logger.info(f"Extracted {len(results)} relevant events from {len(raw_items)} items")
        return results

    except json.JSONDecodeError as e:
        logger.error(f"Event extraction JSON error: {e}")
        logger.error(f"Raw response: {raw[:300]}")
        return []
    except Exception as e:
        logger.error(f"Event extraction error: {e}")
        return []


def extract_events_for_student(student_id: str) -> list:
    """
    Convenience function — loads mock data and extracts events.
    In production this would be called with real scraped + email data.
    """
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    
    from mock_linkedin import get_mock_linkedin_posts
    from mock_xiaohongshu import get_mock_xhs_posts
    
    all_posts = get_mock_linkedin_posts() + get_mock_xhs_posts()
    
    return extract_events(all_posts)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    student_id = sys.argv[1] if len(sys.argv) > 1 else "persona_alex_chen"
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
