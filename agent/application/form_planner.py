"""Build unified application form plans from parsed documents."""

from __future__ import annotations

import json
import logging

from agent.application.docx_parser import extract_form_schema as extract_docx_schema
from agent.application.form_ai import (
    analyze_form_schema_chunked,
    build_filled_data,
    count_fill_targets,
    detect_gaps,
    extract_free_form_fields,
)
from agent.application.pdf_parser import extract_form_schema as extract_pdf_schema

logger = logging.getLogger(__name__)


def plan_has_fill_targets(plan: dict) -> bool:
    table_schema = plan.get("table_schema") or {}
    free_fields = plan.get("free_fields") or []
    return count_fill_targets(table_schema, free_fields) > 0


def build_form_plan(file_path: str, content_type: str, profile: dict) -> dict:
    """Parse a form file and return a unified plan for collection and filling."""
    if content_type == "application/pdf":
        form_json = extract_pdf_schema(file_path)
        doc_format = "pdf"
    else:
        form_json = extract_docx_schema(file_path)
        doc_format = "docx"

    form_payload = json.loads(form_json)
    analysis_errors: list[str] = []

    try:
        table_schema, batch_errors = analyze_form_schema_chunked(form_payload)
        analysis_errors.extend(batch_errors)
    except Exception as exc:
        logger.error("Table schema analysis failed: %s", exc)
        table_schema = {"simple_fields": [], "repeating_lists": [], "long_text": [], "booleans": []}
        analysis_errors.append(str(exc))

    try:
        free_fields = extract_free_form_fields(form_payload)
    except Exception as exc:
        logger.warning("Free-field extraction failed: %s", exc)
        free_fields = []
        analysis_errors.append(f"Free fields: {exc}")

    filled_data = build_filled_data(table_schema, profile, free_fields)
    gaps = detect_gaps(table_schema, filled_data, profile, free_fields)

    plan = {
        "format": doc_format,
        "content_type": content_type,
        "table_schema": table_schema,
        "free_fields": free_fields,
        "filled_data": filled_data,
        "gaps": gaps,
        "form_json": form_json,
        "metadata": {
            "table_count": len(form_payload.get("tables") or []),
            "paragraph_count": len(form_payload.get("paragraphs") or []),
            "widget_count": len(form_payload.get("acroform_widgets") or []),
            "analysis_errors": analysis_errors,
        },
    }
    return plan
