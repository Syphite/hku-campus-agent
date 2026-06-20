"""Extract structured table JSON from PDF application forms."""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def _normalize_cell(value) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text if text else "[Empty]"


def _extract_acroform_widgets(file_path: str) -> list[dict]:
    widgets = []
    try:
        import fitz

        doc = fitz.open(file_path)
        for page_number, page in enumerate(doc):
            for widget in page.widgets() or []:
                name = getattr(widget, "field_name", None) or ""
                if not name:
                    continue
                widgets.append({
                    "field_name": name,
                    "field_type": getattr(widget, "field_type_string", None) or str(getattr(widget, "field_type", "")),
                    "page": page_number,
                })
        doc.close()
    except Exception as exc:
        logger.debug("AcroForm widget scan skipped: %s", exc)
    return widgets


def extract_form_schema(file_path: str) -> str:
    """
    Parse a PDF form into structured JSON for LLM schema analysis.

    Uses pdfplumber table extraction and falls back to line-grouped pseudo-rows.
    """
    import pdfplumber

    tables = []
    paragraphs = []
    table_index = 0

    with pdfplumber.open(file_path) as pdf:
        for page_number, page in enumerate(pdf.pages):
            page_tables = page.extract_tables() or []
            for table in page_tables:
                rows = []
                for row in table or []:
                    cells = [_normalize_cell(cell) for cell in (row or [])]
                    if any(not _normalize_cell(cell) == "[Empty]" for cell in cells):
                        rows.append({"cells": cells})
                if rows:
                    tables.append({
                        "table_index": table_index,
                        "page": page_number,
                        "rows": rows,
                    })
                    table_index += 1

            text = (page.extract_text() or "").strip()
            if text:
                for line_index, line in enumerate(text.splitlines()):
                    cleaned = line.strip()
                    if cleaned:
                        paragraphs.append({
                            "page": page_number,
                            "line_index": line_index,
                            "paragraph_index": len(paragraphs),
                            "text": cleaned,
                        })

            if not page_tables:
                words = page.extract_words(use_text_flow=True) or []
                if words:
                    lines = {}
                    for word in words:
                        top = round(word.get("top", 0), 1)
                        lines.setdefault(top, []).append(word.get("text", ""))
                    pseudo_rows = []
                    for top in sorted(lines.keys()):
                        row_text = " ".join(lines[top]).strip()
                        if row_text:
                            pseudo_rows.append({"cells": [row_text]})
                    if pseudo_rows:
                        tables.append({
                            "table_index": table_index,
                            "page": page_number,
                            "rows": pseudo_rows,
                            "pseudo": True,
                        })
                        table_index += 1

    acroform_widgets = _extract_acroform_widgets(file_path)

    payload = {
        "format": "pdf",
        "source_path": file_path,
        "tables": tables,
        "paragraphs": paragraphs,
        "content_controls": [],
        "acroform_widgets": acroform_widgets,
    }
    logger.info(
        "PDF schema extracted: %s tables, %s paragraph lines, %s widgets",
        len(tables),
        len(paragraphs),
        len(acroform_widgets),
    )
    return json.dumps(payload, ensure_ascii=False, indent=2)
