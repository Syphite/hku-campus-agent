"""Tests for approved-data key resolution and DOCX row detection."""

import unittest
from unittest.mock import MagicMock

from agent.application.docx_filler import _find_header_row, _is_row_label_cell, _next_empty_data_row
from agent.application.form_ai import resolve_approved_repeating_lists


class ResolveApprovedRepeatingListsTests(unittest.TestCase):
    def test_maps_mismatched_pending_key_to_schema_key(self):
        table_schema = {
            "repeating_lists": [
                {
                    "key": "awards_achievements",
                    "label": "Awards / Achievements",
                    "list_kind": "awards",
                    "table_index": 2,
                    "column_headers": ["Date", "Award", "Organization"],
                }
            ]
        }
        pending = {
            "awards": [
                {"dates": "06/24", "role": "Dean's List", "organization": "HKU"},
            ]
        }
        gap_queue = [
            {
                "key": "awards",
                "label": "Awards / Achievements",
                "schema": {
                    "key": "awards",
                    "list_kind": "awards",
                    "table_index": 2,
                },
            }
        ]

        resolved, _warnings = resolve_approved_repeating_lists(table_schema, pending, gap_queue)

        self.assertIn("awards_achievements", resolved)
        self.assertEqual(len(resolved["awards_achievements"]), 1)
        self.assertEqual(resolved["awards_achievements"][0]["role"], "Dean's List")


class DocxRowDetectionTests(unittest.TestCase):
    def test_row_label_cells_recognized(self):
        self.assertTrue(_is_row_label_cell("C1"))
        self.assertTrue(_is_row_label_cell("f2"))
        self.assertFalse(_is_row_label_cell("Award"))

    def test_next_empty_data_row_skips_row_label_column(self):
        table = MagicMock()
        table.rows = [MagicMock(), MagicMock(), MagicMock()]

        header_cells = [MagicMock(), MagicMock(), MagicMock()]
        header_cells[0].text = "Date"
        header_cells[1].text = "Award"
        header_cells[2].text = "Organization"

        data_cells = [MagicMock(), MagicMock(), MagicMock()]
        data_cells[0].text = "C1"
        data_cells[1].text = ""
        data_cells[2].text = ""

        table.rows[0].cells = header_cells
        table.rows[1].cells = data_cells

        def row_cells_side_effect(_table, row_index):
            if row_index == 0:
                return header_cells
            if row_index == 1:
                return data_cells
            return []

        column_map = {"dates": 1, "role": 2}

        import agent.application.docx_filler as docx_filler

        original = docx_filler._row_cells
        docx_filler._row_cells = row_cells_side_effect
        try:
            match = _next_empty_data_row(table, 0, column_map, 5)
        finally:
            docx_filler._row_cells = original

        self.assertIsNotNone(match)
        row_index, _cells = match
        self.assertEqual(row_index, 1)

    def test_find_header_row_requires_two_header_matches(self):
        table = MagicMock()
        table.rows = [MagicMock(), MagicMock()]

        instruction_cells = [MagicMock()]
        instruction_cells[0].text = "Section C: list awards below with Date and Award columns"

        header_cells = [MagicMock(), MagicMock(), MagicMock()]
        header_cells[0].text = "Date (MM/YY)"
        header_cells[1].text = "Name of Award"
        header_cells[2].text = "Organization"

        table.rows[0].cells = instruction_cells
        table.rows[1].cells = header_cells

        import agent.application.docx_filler as docx_filler

        original = docx_filler._row_cells

        def row_cells_side_effect(_table, row_index):
            if row_index == 0:
                return instruction_cells
            if row_index == 1:
                return header_cells
            return []

        docx_filler._row_cells = row_cells_side_effect
        try:
            match = _find_header_row(
                table,
                ["Date (MM/YY)", "Name of Award", "Organization"],
            )
        finally:
            docx_filler._row_cells = original

        self.assertIsNotNone(match)
        row_index, _cells = match
        self.assertEqual(row_index, 1)


if __name__ == "__main__":
    unittest.main()
