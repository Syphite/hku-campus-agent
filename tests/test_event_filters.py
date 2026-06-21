"""Tests for filtering past-deadline events."""

import unittest
from datetime import date

from agent.events.event_filters import filter_open_events, is_event_still_open


class EventFilterTests(unittest.TestCase):
    def test_rejects_past_deadline(self):
        event = {"title": "Old hackathon", "deadline": "2020-01-01"}
        self.assertFalse(is_event_still_open(event, today=date(2026, 6, 21)))

    def test_keeps_future_deadline(self):
        event = {"title": "Future hackathon", "deadline": "2026-07-01"}
        self.assertTrue(is_event_still_open(event, today=date(2026, 6, 21)))

    def test_keeps_today_deadline(self):
        event = {"title": "Due today", "deadline": "2026-06-21"}
        self.assertTrue(is_event_still_open(event, today=date(2026, 6, 21)))

    def test_keeps_no_deadline(self):
        event = {"title": "Open event"}
        self.assertTrue(is_event_still_open(event, today=date(2026, 6, 21)))

    def test_uses_calendar_end_when_no_deadline(self):
        event = {
            "title": "Past finals",
            "calendar_end_iso": "2026-06-20T19:00:00",
        }
        self.assertFalse(is_event_still_open(event, today=date(2026, 6, 21)))

    def test_filter_open_events(self):
        events = [
            {"title": "Past", "deadline": "2025-01-01"},
            {"title": "Future", "deadline": "2026-08-01"},
        ]
        filtered = filter_open_events(events, today=date(2026, 6, 21))
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["title"], "Future")


if __name__ == "__main__":
    unittest.main()
