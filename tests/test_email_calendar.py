"""Tests for inbox action step normalization."""

import unittest

from agent.email_calendar import _normalize_action_step


class ActionStepNormalizationTests(unittest.TestCase):
    def test_strips_step_prefix(self):
        self.assertEqual(
            _normalize_action_step("Step 1: Review the email content and identify resources."),
            "Review the email content and identify resources.",
        )

    def test_strips_numeric_prefix(self):
        self.assertEqual(
            _normalize_action_step("1. Allocate time to practice numerical reasoning assessments."),
            "Allocate time to practice numerical reasoning assessments.",
        )


if __name__ == "__main__":
    unittest.main()
