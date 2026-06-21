"""Tests for personalized long-text auto-draft and profile context."""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# Stub Cosmos before agent.profile (and handler) import — no live DB needed.
_mock_cosmos_mod = types.ModuleType("azure.cosmos")
_mock_exceptions = types.ModuleType("azure.cosmos.exceptions")
_mock_exceptions.CosmosResourceNotFoundError = type("CosmosResourceNotFoundError", (Exception,), {})
_mock_cosmos_mod.CosmosClient = MagicMock(return_value=MagicMock())
_mock_cosmos_mod.exceptions = _mock_exceptions
sys.modules.setdefault("azure.cosmos", _mock_cosmos_mod)
sys.modules.setdefault("azure.cosmos.exceptions", _mock_exceptions)

_mock_blob_mod = types.ModuleType("azure.storage.blob")
_mock_blob_mod.BlobSasPermissions = MagicMock()
_mock_blob_mod.BlobServiceClient = MagicMock()
_mock_blob_mod.generate_blob_sas = MagicMock()
sys.modules.setdefault("azure.storage.blob", _mock_blob_mod)

for _env_key, _env_val in {
    "COSMOS_ENDPOINT": "https://example.com",
    "COSMOS_KEY": "test-key",
    "COSMOS_DATABASE": "test-db",
    "AZURE_OPENAI_ENDPOINT": "https://example.com",
    "AZURE_OPENAI_API_KEY": "test-key",
}.items():
    os.environ.setdefault(_env_key, _env_val)

from agent.application.form_ai import (
    build_filled_data,
    build_profile_context_for_long_text_draft,
    draft_long_text,
)
from agent.handler import _begin_application_review


SAMPLE_PROFILE = {
    "name": "Alex Chan",
    "interests": ["AI", "robotics"],
    "activities": ["HKU Robotics Team", "Hackathon participant"],
    "cv_text": "Dean's List 2024. Built autonomous rover for HKU Engineering Expo.",
    "academic": {
        "gpa": "3.8",
        "faculty": "Engineering",
        "programme": "BEng(CS)",
        "year_of_study": 2,
    },
}


class BuildProfileContextForLongTextDraftTests(unittest.TestCase):
    def test_includes_gpa_interests_activities_cv_and_academic_context(self):
        context = build_profile_context_for_long_text_draft(SAMPLE_PROFILE)

        self.assertEqual(context["gpa"], "3.8")
        self.assertEqual(context["interests"], ["AI", "robotics"])
        self.assertEqual(context["activities"], ["HKU Robotics Team", "Hackathon participant"])
        self.assertIn("Dean's List", context["cv_text"])
        self.assertEqual(context["faculty"], "Engineering")
        self.assertIn("Computer Science", context["programme"])
        self.assertEqual(context["year_of_study"], "2")

    def test_cv_text_truncated_to_3500_chars(self):
        profile = dict(SAMPLE_PROFILE)
        profile["cv_text"] = "x" * 5000
        context = build_profile_context_for_long_text_draft(profile)
        self.assertEqual(len(context["cv_text"]), 3500)


class BuildFilledDataLongTextTests(unittest.TestCase):
    def test_long_text_fields_start_empty_not_static_boilerplate(self):
        schema = {
            "simple_fields": [],
            "repeating_lists": [],
            "long_text": [
                {"key": "personal_statement", "anchor_label": "Personal Statement"},
                {"key": "motivation_essay", "anchor_label": "Why this scholarship"},
            ],
            "booleans": [],
        }
        filled = build_filled_data(schema, SAMPLE_PROFILE)

        self.assertEqual(filled["long_text"]["personal_statement"], "")
        self.assertEqual(filled["long_text"]["motivation_essay"], "")


class DraftLongTextPromptTests(unittest.TestCase):
    @patch("agent.application.form_ai._get_openai_client")
    def test_draft_long_text_passes_curated_profile_context(self, mock_client_factory):
        mock_response = unittest.mock.MagicMock()
        mock_response.choices = [
            unittest.mock.MagicMock(message=unittest.mock.MagicMock(content='{"text": "Draft body"}'))
        ]
        mock_client = unittest.mock.MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        mock_client_factory.return_value = mock_client

        field_schema = {"key": "personal_statement", "anchor_label": "Personal Statement", "target_words": 400}
        result = draft_long_text(field_schema, SAMPLE_PROFILE, {"awards": []})

        self.assertEqual(result, "Draft body")
        prompt = mock_client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        self.assertIn('"gpa": "3.8"', prompt)
        self.assertIn("HKU Robotics Team", prompt)
        self.assertIn("Dean's List", prompt)
        self.assertIn("AI", prompt)
        self.assertNotIn('"student_number"', prompt)


class BeginApplicationReviewAutoDraftTests(unittest.TestCase):
    @patch("agent.handler.draft_long_text", return_value="Personalized draft from profile.")
    @patch("agent.handler.save_profile")
    @patch("agent.handler.update_application_state")
    def test_auto_drafts_unfilled_long_text_gaps(self, _mock_update, _mock_save, mock_draft):
        gap = {
            "type": "long_text",
            "key": "personal_statement",
            "label": "Personal Statement",
            "schema": {"key": "personal_statement", "target_words": 800},
        }
        state = {
            "gap_queue": [gap],
            "skipped_sections": [],
            "long_text_drafts": {},
            "ai_drafted_keys": [],
            "pending_list_data": {"awards": [{"role": "Dean's List"}]},
            "scholarship_id": "ss_472",
            "input_path": "/tmp/form.docx",
            "output_path": "/tmp/out.docx",
            "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        profile = {"student_id": "test-user", **SAMPLE_PROFILE}

        responses = _begin_application_review(profile, state)

        mock_draft.assert_called_once()
        draft_schema, draft_profile, pending_lists = mock_draft.call_args[0]
        self.assertEqual(draft_schema["anchor_label"], "Personal Statement")
        self.assertEqual(draft_profile["name"], "Alex Chan")
        self.assertEqual(pending_lists, {"awards": [{"role": "Dean's List"}]})
        self.assertEqual(state["long_text_drafts"]["personal_statement"], "Personalized draft from profile.")
        self.assertIn("personal_statement", state["ai_drafted_keys"])
        self.assertEqual(state["step"], "review")
        self.assertTrue(responses)

    @patch("agent.handler.draft_long_text")
    @patch("agent.handler.save_profile")
    @patch("agent.handler.update_application_state")
    def test_skips_long_text_gaps_already_drafted_or_skipped(self, _mock_update, _mock_save, mock_draft):
        gap = {
            "type": "long_text",
            "key": "essay",
            "label": "Essay",
            "schema": {"key": "essay"},
        }
        state = {
            "gap_queue": [gap],
            "skipped_sections": ["essay"],
            "long_text_drafts": {},
            "ai_drafted_keys": [],
            "pending_list_data": {},
            "scholarship_id": "ss_472",
        }
        profile = {"student_id": "test-user"}

        _begin_application_review(profile, state)
        mock_draft.assert_not_called()

        state["skipped_sections"] = []
        state["long_text_drafts"] = {"essay": "Already written."}
        _begin_application_review(profile, state)
        mock_draft.assert_not_called()


if __name__ == "__main__":
    unittest.main()
