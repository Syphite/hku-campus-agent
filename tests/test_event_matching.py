"""Tests for event year and interest-based matching."""

import unittest

from agent.events.event_matching import (
    _event_year_eligible,
    _fallback_match_reason,
    _passes_profile_fit,
    _reason_is_profile_grounded,
    _year_matches,
)


class EventYearMatchingTests(unittest.TestCase):
    def test_year_tags_do_not_allow_all_to_override_final_year_only(self):
        post = {"year_tags": ["4", "master", "all"]}
        self.assertFalse(_year_matches(post, "year 1"))
        self.assertTrue(_year_matches(post, "year 4"))

    def test_pwc_graduate_event_not_eligible_for_year_1(self):
        event = {
            "title": "PwC Graduate Programme 2027",
            "summary": "Final year undergraduates and fresh graduates.",
            "eligibility": "Final year undergraduates and fresh graduates.",
            "year_relevant": ["4", "master", "all"],
        }
        profile = {
            "academic": {"year_of_study": "Year 1", "faculty": "School of Computing and Data Science"},
            "interests": ["consulting"],
        }
        self.assertFalse(_event_year_eligible(event, profile))


class EventReasoningTests(unittest.TestCase):
    def test_faculty_only_reason_rejected(self):
        profile = {"interests": ["robotics"], "activities": []}
        reason = "Open to HKU students in School Of Computing And Data Science."
        self.assertFalse(_reason_is_profile_grounded(reason, profile))

    def test_interest_reason_accepted(self):
        profile = {"interests": ["consulting"], "activities": []}
        reason = "Matches your interest in consulting and career development."
        self.assertTrue(_reason_is_profile_grounded(reason, profile))

    def test_fallback_reason_prefers_interests(self):
        event = {"title": "AI Hackathon", "summary": "Robotics and AI competition", "eligibility": "All years"}
        profile = {"interests": ["robotics", "AI"], "activities": []}
        reason = _fallback_match_reason(event, profile, "ai hackathon robotics and ai competition")
        self.assertIn("interest", reason.lower())

    def test_profile_fit_requires_interest_not_faculty_only(self):
        event = {
            "title": "PwC Graduate Programme 2027",
            "summary": "Big four graduate intake",
            "eligibility": "Final year undergraduates",
            "year_relevant": ["4", "master"],
        }
        profile = {
            "academic": {"year_of_study": "Year 1", "faculty": "School of Computing and Data Science"},
            "interests": [],
            "activities": [],
        }
        self.assertFalse(_passes_profile_fit(event, profile))


if __name__ == "__main__":
    unittest.main()
