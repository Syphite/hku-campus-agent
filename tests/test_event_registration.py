"""Tests for event calendar schedule parsing and registration impact."""

import unittest
from unittest.mock import patch

from agent.datetime_utils import parse_graph_datetime_field
from agent.event_registration import (
    _build_calendar_fact,
    _fallback_registration_warnings,
    _parse_cn_datetime_range,
    _parse_local_dt,
    _rule_based_calendar_decisions,
    assess_registration_impact,
    normalize_event_calendar_fields,
    resolve_event_schedule,
)


class GraphTimezoneTests(unittest.TestCase):
    def test_utc_deadline_converts_to_hkt(self):
        field = {"dateTime": "2026-06-25T13:00:00.0000000", "timeZone": "UTC"}
        dt = parse_graph_datetime_field(field)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.hour, 21)
        self.assertEqual(dt.minute, 0)

    def test_hkt_event_keeps_local_time(self):
        field = {"dateTime": "2026-06-24T19:00:00", "timeZone": "Asia/Hong_Kong"}
        dt = parse_graph_datetime_field(field)
        self.assertEqual(dt.hour, 19)


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
    def test_utc_deadline_does_not_false_overlap(self):
        reg_start = _parse_local_dt("2026-06-24T19:00:00")
        reg_end = _parse_local_dt("2026-06-25T19:00:00")
        cal_event = {
            "id": "deadline-1",
            "subject": "Writing Competition Deadline",
            "start": {"dateTime": "2026-06-25T13:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-06-25T14:00:00", "timeZone": "UTC"},
        }
        fact = _build_calendar_fact(cal_event, reg_start, reg_end)
        self.assertIsNotNone(fact)
        self.assertFalse(fact["overlaps_registration"])
        self.assertEqual(fact["end_hkt"], "2026-06-25T22:00:00")

        decisions = _rule_based_calendar_decisions([fact])
        self.assertEqual(decisions["replace_events"], [])
        self.assertEqual(decisions["hard_conflict_warnings"], [])

    def test_flags_gathering_two_hours_before_start(self):
        reg_start = _parse_local_dt("2026-06-24T19:00:00")
        reg_end = _parse_local_dt("2026-06-25T19:00:00")
        cal_event = {
            "id": "gathering-1",
            "subject": "Team gathering",
            "start": {"dateTime": "2026-06-24T17:00:00", "timeZone": "Asia/Hong_Kong"},
            "end": {"dateTime": "2026-06-24T17:30:00", "timeZone": "Asia/Hong_Kong"},
        }
        fact = _build_calendar_fact(cal_event, reg_start, reg_end)
        self.assertIsNotNone(fact)
        self.assertFalse(fact["overlaps_registration"])
        self.assertEqual(fact["hours_before_registration_start"], 1.5)

        warnings = _fallback_registration_warnings(
            [fact],
            [],
            title="深港澳AI创新大赛2026",
            reg_start=reg_start,
            reg_end=reg_end,
        )
        self.assertTrue(any("Team gathering" in w for w in warnings))
        self.assertTrue(any("double-check" in w.lower() for w in warnings))

    def test_hard_conflict_marks_replaceable_event(self):
        reg_start = _parse_local_dt("2026-06-24T19:00:00")
        reg_end = _parse_local_dt("2026-06-25T19:00:00")
        cal_event = {
            "id": "clash-1",
            "subject": "Dinner with friends",
            "start": {"dateTime": "2026-06-24T18:00:00", "timeZone": "Asia/Hong_Kong"},
            "end": {"dateTime": "2026-06-24T20:00:00", "timeZone": "Asia/Hong_Kong"},
        }
        fact = _build_calendar_fact(cal_event, reg_start, reg_end)
        decisions = _rule_based_calendar_decisions([fact])
        self.assertEqual(len(decisions["replace_events"]), 1)
        self.assertEqual(decisions["replace_event_ids"], ["clash-1"])

    @patch("agent.event_registration.get_calendar_events")
    @patch("agent.event_registration._llm_registration_warnings", return_value=[])
    def test_assess_with_mock_calendar(self, _mock_llm, mock_get_calendar):
        mock_get_calendar.return_value = {
            "success": True,
            "events": [
                {
                    "id": "deadline-1",
                    "subject": "Writing Competition Deadline",
                    "start": {"dateTime": "2026-06-25T13:00:00", "timeZone": "UTC"},
                    "end": {"dateTime": "2026-06-25T14:00:00", "timeZone": "UTC"},
                },
                {
                    "id": "gathering-1",
                    "subject": "Team gathering",
                    "start": {"dateTime": "2026-06-24T17:00:00", "timeZone": "Asia/Hong_Kong"},
                    "end": {"dateTime": "2026-06-24T17:30:00", "timeZone": "Asia/Hong_Kong"},
                },
            ],
        }
        profile = {"timetable": {"upcoming_deadlines": []}}
        impact = assess_registration_impact(
            "token",
            profile,
            title="深港澳AI创新大赛2026",
            start_iso="2026-06-24T19:00:00",
            end_iso="2026-06-25T19:00:00",
        )
        self.assertEqual(impact["replace_events"], [])
        joined = " ".join(impact["warnings"])
        self.assertNotIn("overlaps in time", joined.lower())
        self.assertTrue(any("Team gathering" in w for w in impact["warnings"]))


if __name__ == "__main__":
    unittest.main()
