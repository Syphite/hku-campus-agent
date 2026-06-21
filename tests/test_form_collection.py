"""Tests for scholarship form collection parsing (no Azure/Cosmos required)."""

import unittest

from agent.application.form_ai import (
    build_filled_data,
    format_list_entries_summary,
    parse_application_collection,
    parse_education_entries_heuristic,
    parse_list_entries_batch,
    parse_list_entry,
)
from agent.application.profile_resolver import is_email_form_field, resolve_profile_field


EDUCATION_SCHEMA = {
    "key": "education_history",
    "list_kind": "education",
    "column_headers": ["Period", "Name of School / Institution", "Qualification Obtained / To Be Obtained"],
    "item_fields": {
        "dates": "Period",
        "institution": "Name of School / Institution",
        "qualification": "Qualification Obtained / To Be Obtained",
    },
    "max_rows": 5,
}

EDUCATION_GAP = {
    "type": "repeating_list",
    "key": "education_history",
    "label": "Education / Academic Background",
    "schema": EDUCATION_SCHEMA,
}


class EmailAutofillTests(unittest.TestCase):
    def test_email_fields_are_not_resolved_from_profile(self):
        profile = {
            "name": "Alex Chan",
            "email": "alex.chan@connect.hku.hk",
            "university_email": "3030123456@connect.hku.hk",
            "personal_email": "alex@gmail.com",
        }
        email_fields = [
            {"key": "email", "profile_key": "email", "anchor_label": "Email Address"},
            {"key": "university_email", "profile_key": "university_email", "anchor_label": "University Email"},
            {"key": "personal_email", "anchor_label": "Personal Email"},
        ]
        for field in email_fields:
            self.assertTrue(is_email_form_field(field))
            self.assertEqual(resolve_profile_field(profile, field), "")

    def test_build_filled_data_skips_email_simple_fields(self):
        profile = {
            "name": "Alex Chan",
            "email": "alex.chan@connect.hku.hk",
            "academic": {"programme": "BEng(CS)", "faculty": "Engineering"},
        }
        schema = {
            "simple_fields": [
                {"key": "name", "profile_key": "name", "anchor_label": "Name"},
                {"key": "email", "profile_key": "email", "anchor_label": "Email Address"},
                {"key": "programme", "profile_key": "programme", "anchor_label": "Programme"},
            ],
            "repeating_lists": [],
            "long_text": [],
            "booleans": [],
        }
        filled = build_filled_data(schema, profile)
        simple = filled["simple_fields"]
        self.assertEqual(simple.get("name"), "Alex Chan")
        self.assertNotIn("email", simple)
        self.assertIn("programme", simple)


class EducationHeuristicTests(unittest.TestCase):
    def test_comma_separated_education_line(self):
        entries = parse_education_entries_heuristic(
            "8/2023-5/2025, Sha Tin College, IBDP",
            EDUCATION_SCHEMA,
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["dates"], "8/2023-5/2025")
        self.assertEqual(entries[0]["institution"], "Sha Tin College")
        self.assertEqual(entries[0]["qualification"], "IBDP")

    def test_free_text_education_line(self):
        entries = parse_education_entries_heuristic(
            "Sha Tin College IBDP 2023-2025",
            EDUCATION_SCHEMA,
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["institution"], "Sha Tin College")
        self.assertEqual(entries[0]["qualification"], "IBDP")
        self.assertEqual(entries[0]["dates"], "2023-2025")

    def test_a_level_pattern(self):
        entries = parse_education_entries_heuristic(
            "King's College, A-level, 2020-2022",
            EDUCATION_SCHEMA,
        )
        self.assertEqual(len(entries), 1)
        self.assertIn("King", entries[0]["institution"])
        self.assertEqual(entries[0]["qualification"], "A-level")
        self.assertEqual(entries[0]["dates"], "2020-2022")

    def test_parse_list_entry_uses_education_fields(self):
        entry = parse_list_entry("8/2023-5/2025, Sha Tin College, IBDP", EDUCATION_SCHEMA)
        self.assertEqual(entry["institution"], "Sha Tin College")
        self.assertEqual(entry["qualification"], "IBDP")
        self.assertEqual(entry["dates"], "8/2023-5/2025")

    def test_parse_list_entries_batch_without_llm(self):
        entries = parse_list_entries_batch(
            "8/2023-5/2025, Sha Tin College, IBDP",
            EDUCATION_SCHEMA,
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["institution"], "Sha Tin College")

    def test_format_list_entries_summary(self):
        entries = parse_education_entries_heuristic(
            "8/2023-5/2025, Sha Tin College, IBDP",
            EDUCATION_SCHEMA,
        )
        summary = format_list_entries_summary(entries, EDUCATION_SCHEMA)
        self.assertIn("Sha Tin College", summary)
        self.assertIn("IBDP", summary)
        self.assertIn("8/2023-5/2025", summary)


class ApplicationCollectionRouterTests(unittest.TestCase):
    def test_education_entry_overrides_clarify_intent(self):
        parsed = {
            "intent": "clarify",
            "extracted_data": {},
            "agent_response": "Thanks for starting! Could you add details of any other previous tertiary or secondary education?",
        }

        class FakeCompletions:
            def create(self, **_kwargs):
                class Message:
                    content = __import__("json").dumps(parsed)

                class Choice:
                    message = Message()

                class Response:
                    choices = [Choice()]

                return Response()

        class FakeClient:
            chat = type("Chat", (), {"completions": FakeCompletions()})()

        import agent.application.form_ai as form_ai

        original_client = form_ai._get_openai_client
        form_ai._get_openai_client = lambda: FakeClient()
        try:
            result = parse_application_collection(
                "8/2023-5/2025, Sha Tin College, IBDP",
                EDUCATION_GAP,
                profile={},
                state={},
            )
        finally:
            form_ai._get_openai_client = original_client

        self.assertEqual(result["intent"], "fill_section")
        self.assertEqual(
            result["extracted_data"]["answer_text"],
            "8/2023-5/2025, Sha Tin College, IBDP",
        )


if __name__ == "__main__":
    unittest.main()
