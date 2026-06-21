"""Tests for event calendar schedule parsing and registration impact."""

import unittest

from agent.event_registration import (
    _parse_cn_datetime_range,
    assess_registration_impact,
    normalize_event_calendar_fields,
    resolve_event_schedule,
)


class EventScheduleParsingTests(unittest.TestCase):
    def test_parses_chinese_multi_day_range(self):
        text = "线下决赛：6月24日19:00至6月25日19:00（香港时间）。"
        start_iso, end_iso = _parse_cn_datetime_range(text)
        self.assertEqual(start_iso, "2026-06-24T19:00:00")
        self.assertEqual(end_iso, "2026-06-25T19:00:00")

    def test_normalizes_from_source_text(self):
        event = {
            "title": "深港澳AI创新大赛2026",
            "_source_text": "线下决赛：6月24日19:00至6月25日19:00（香港时间）。",
            "event_sessions": [],
        }
        normalized = normalize_event_calendar_fields(event)
        schedule = resolve_event_schedule(normalized)
        self.assertIsNotNone(schedule)
        self.assertEqual(schedule["start_iso"], "2026-06-24T19:00:00")
        self.assertEqual(schedule["end_iso"], "2026-06-25T19:00:00")
        self.assertIn("7:00 PM", schedule["display_time"])


class RegistrationImpactTests(unittest.TestCase):
    def test_flags_gathering_but_keeps_deadline(self):
        profile = {"timetable": {"upcoming_deadlines": []}}
        impact = assess_registration_impact(
            None,
            profile,
            title="深港澳AI创新大赛2026",
            start_iso="2026-06-24T19:00:00",
            end_iso="2026-06-25T19:00:00",
        )
        self.assertEqual(impact["replace_events"], [])


if __name__ == "__main__":
    unittest.main()
