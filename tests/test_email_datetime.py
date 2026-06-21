"""Tests for email date/time extraction and event dedupe."""

import unittest

from agent.email_calendar import extract_event_session
from agent.email_events import dedupe_events, inbox_items_to_events


class EmailDateTimeExtractionTests(unittest.TestCase):
    def test_extracts_date_time_line_with_range(self):
        preview = (
            "Public Seminar and Oral Examination\n"
            "Date / Time: June 18 2026 (Thu) 3:00 PM – 4:00 PM\n"
            "Venue: Lecture Theatre"
        )
        timing = extract_event_session("Chemistry seminar", preview)
        self.assertIsNotNone(timing)
        self.assertEqual(timing.get("kind"), "event")
        self.assertIn("June 18 2026", timing.get("deadline_display", ""))
        self.assertIn("3:00 PM", timing.get("deadline_display", ""))
        self.assertIn("4:00 PM", timing.get("deadline_display", ""))
        self.assertTrue(timing.get("start_iso"))
        self.assertTrue(timing.get("end_iso"))


class EmailEventDedupeTests(unittest.TestCase):
    def test_inbox_items_to_events_dedupes_same_subject(self):
        items = [
            {
                "email_id": "a1",
                "subject": "Seminar",
                "from": "chem@hku.hk",
                "body_preview": "Date / Time: June 18 2026 (Thu) 3:00 PM – 4:00 PM",
                "timing": {"kind": "event", "deadline_display": "June 18"},
                "reason": "relevant",
            },
            {
                "email_id": "a2",
                "subject": "Seminar",
                "from": "chem@hku.hk",
                "body_preview": "duplicate thread",
                "timing": {"kind": "event", "deadline_display": "June 18"},
                "reason": "relevant",
            },
        ]
        events = inbox_items_to_events([], items)
        self.assertEqual(len(events), 1)

    def test_dedupe_events_by_email_id(self):
        events = [
            {"source": "email", "email_id": "x", "title": "Talk A"},
            {"source": "email", "email_id": "x", "title": "Talk A"},
            {"source": "linkedin", "source_id": "ev1", "title": "Hackathon"},
        ]
        deduped = dedupe_events(events)
        self.assertEqual(len(deduped), 2)


if __name__ == "__main__":
    unittest.main()
