"""Tests for inbox email classification heuristics."""

import unittest

from agent.classifier import classify_email, heuristic_classify


PROFILE = {
    "interests": ["AI", "robotics"],
    "courses": ["COMP2113"],
    "academic": {
        "programme": "Bachelor of Science in Computer Science",
        "year_of_study": 2,
        "faculty": "School of Computing and Data Science",
    },
}


class HeuristicClassifierTests(unittest.TestCase):
    def test_hku_bulk_mail_routes_to_ambiguous(self):
        result = heuristic_classify(
            "Weekly campus newsletter",
            "Upcoming events across all faculties.",
            "news@hku.hk",
            PROFILE,
        )
        self.assertEqual(result["label"], "ambiguous")
        self.assertFalse(result["decisive"])

    def test_external_promo_routes_to_noise(self):
        result = heuristic_classify(
            "50% off sale today",
            "Limited time discount — buy now and unsubscribe here.",
            "promo@shop.example.com",
            PROFILE,
        )
        self.assertEqual(result["label"], "noise")
        self.assertTrue(result["decisive"])

    def test_strong_profile_match_routes_to_relevant(self):
        result = heuristic_classify(
            "AI robotics hackathon",
            "Calling all AI and robotics students to join the competition.",
            "events@hku.hk",
            PROFILE,
        )
        self.assertEqual(result["label"], "relevant")
        self.assertTrue(result["decisive"])

    def test_single_profile_hit_is_ambiguous_not_auto_relevant(self):
        result = heuristic_classify(
            "Robotics club social",
            "Casual meetup for robotics enthusiasts.",
            "club@hku.hk",
            PROFILE,
        )
        self.assertEqual(result["label"], "ambiguous")
        self.assertFalse(result["decisive"])

    def test_asso_forum_without_match_is_ambiguous(self):
        result = heuristic_classify(
            "ASSO_FORUM: General notice",
            "Campus-wide announcement for all students.",
            "asso_forum@hku.hk",
            PROFILE,
        )
        self.assertEqual(result["label"], "ambiguous")

    def test_urgent_hku_mail_is_urgent(self):
        result = heuristic_classify(
            "Course registration deadline tomorrow",
            "Final chance to add or drop courses.",
            "registry@hku.hk",
            PROFILE,
        )
        self.assertEqual(result["label"], "urgent")
        self.assertTrue(result["decisive"])

    def test_classify_empty_email_is_noise(self):
        result = classify_email("", "", "", PROFILE)
        self.assertEqual(result["label"], "noise")


    def test_catholic_society_not_relevant_for_ai_lab_profile(self):
        profile = {
            "interests": ["AI", "robotics"],
            "academic": {"programme": "Bachelor of Science in Computer Science"},
        }
        subject = "Catholic Society- Lecture Series , June Talk | 六月講座"
        preview = "Monthly lecture series for students."
        result = classify_email(subject, preview, "club@hku.hk", profile)
        self.assertNotEqual(result["label"], "relevant")
        self.assertNotIn("ai, lab", result.get("reason", "").lower())

    def test_keyword_in_text_rejects_ai_inside_catholic(self):
        from agent.classifier import _keyword_in_text
        self.assertFalse(_keyword_in_text("ai", "Catholic Society lecture series"))


if __name__ == "__main__":
    unittest.main()
