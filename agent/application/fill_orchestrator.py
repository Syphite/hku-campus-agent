"""Route table and free-field filling to the appropriate tools."""

from __future__ import annotations

import logging
import os
import shutil

from agent.application.cell_utils import normalize_text
from agent.application.docx_filler import fill_docx_form, fill_free_fields_docx
from agent.application.pdf_filler import fill_pdf_form

logger = logging.getLogger(__name__)


def _fill_pdf_acroform_fields(
    pdf_path: str,
    free_field_defs: list[dict],
    free_field_values: dict,
) -> int:
    """Fill PDF AcroForm widgets mapped in free_field_defs."""
    try:
        import fitz
    except ImportError:
        logger.error("PyMuPDF is not installed for AcroForm filling")
        return 0

    if not free_field_values:
        return 0

    filled = 0
    doc = fitz.open(pdf_path)
    widget_map = {}
    for page in doc:
        for widget in page.widgets() or []:
            name = getattr(widget, "field_name", None)
            if name:
                widget_map[normalize_text(name)] = widget

    for field in free_field_defs or []:
        key = field.get("key")
        value = str((free_field_values or {}).get(key) or "").strip()
        if not value:
            continue
        field_name = field.get("field_name")
        if not field_name:
            continue
        widget = widget_map.get(normalize_text(field_name))
        if widget is None:
            for name, candidate in widget_map.items():
                if normalize_text(field_name) in name or name in normalize_text(field_name):
                    widget = candidate
                    break
        if widget is None:
            continue
        widget.field_value = value[:4000]
        widget.update()
        filled += 1

    doc.save(pdf_path)
    doc.close()
    return filled


def fill_application(
    plan: dict,
    input_path: str,
    output_path: str,
    merged_data: dict,
    content_type: str,
) -> str:
    """
    Fill an application form using table schema and free-field definitions.

    Returns the output file path.
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(input_path)

    table_schema = plan.get("table_schema") or {}
    free_field_defs = plan.get("free_fields") or []
    free_field_values = merged_data.get("free_fields") or {}

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    shutil.copy2(input_path, output_path)

    if content_type == "application/pdf":
        fill_pdf_form(input_path, merged_data, table_schema, output_path)
        _fill_pdf_acroform_fields(output_path, free_field_defs, free_field_values)
    else:
        fill_docx_form(input_path, merged_data, table_schema, output_path)
        fill_free_fields_docx(output_path, free_field_defs, free_field_values)

    if not os.path.exists(output_path):
        raise RuntimeError("Filled form was not created")
    return output_path
