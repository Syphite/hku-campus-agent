"""Tests for legacy form_filler field-specific answer matching."""

import unittest

from agent.form_filler import _field_specific_answer


class FieldSpecificAnswerTests(unittest.TestCase):
    def test_matches_form_fields_by_key(self):
        scholarship = {
            "form_fields": {
                "motivation_essay": "I want to study AI ethics.",
            }
        }
        self.assertEqual(
            _field_specific_answer("motivation_essay_field", scholarship),
            "I want to study AI ethics.",
        )

    def test_matches_long_text_dict_by_key(self):
        scholarship = {
            "long_text": {
                "reason_for_applying": "Because of my robotics work.",
            }
        }
        self.assertEqual(
            _field_specific_answer("reason_for_applying", scholarship),
            "Because of my robotics work.",
        )

    def test_does_not_use_cover_letter_blob(self):
        scholarship = {
            "drafted_cover_letter": "Dear committee...",
            "application_answers_text": "Generic blob text",
        }
        self.assertEqual(_field_specific_answer("personal_statement", scholarship), "")

    def test_empty_when_no_explicit_answer(self):
        self.assertEqual(_field_specific_answer("essay_response", {}), "")


if __name__ == "__main__":
    unittest.main()
