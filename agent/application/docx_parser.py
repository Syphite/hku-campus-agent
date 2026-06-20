"""Extract structured table JSON from DOCX application forms."""

import json
import logging

from agent.application.cell_utils import dedupe_row_cells

logger = logging.getLogger(__name__)


def extract_form_schema(file_path: str) -> str:
    """
    Parse a DOCX form into structured JSON for LLM schema analysis.

    Returns a JSON string with tables and standalone paragraphs.
    """
    from docx import Document

    doc = Document(file_path)
    tables = []

    for table_index, table in enumerate(doc.tables):
        rows = []
        for row in table.rows:
            raw_cells = [cell.text.replace("\n", " ").strip() for cell in row.cells]
            cells = dedupe_row_cells(raw_cells)
            rows.append({
                "cells": [cell if cell else "[Empty]" for cell in cells]
            })
        tables.append({"table_index": table_index, "rows": rows})

    paragraphs = []
    for index, paragraph in enumerate(doc.paragraphs):
        text = paragraph.text.replace("\n", " ").strip()
        if text:
            paragraphs.append({"paragraph_index": index, "text": text})

    payload = {
        "format": "docx",
        "source_path": file_path,
        "tables": tables,
        "paragraphs": paragraphs,
    }
    logger.info(
        "DOCX schema extracted: %s tables, %s paragraphs",
        len(tables),
        len(paragraphs),
    )
    return json.dumps(payload, ensure_ascii=False, indent=2)
