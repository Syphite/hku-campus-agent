"""Extract structured table JSON from PDF application forms."""

import json
import logging

logger = logging.getLogger(__name__)


def _normalize_cell(value) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text if text else "[Empty]"


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

    payload = {
        "format": "pdf",
        "source_path": file_path,
        "tables": tables,
        "paragraphs": paragraphs,
    }
    logger.info(
        "PDF schema extracted: %s tables, %s paragraph lines",
        len(tables),
        len(paragraphs),
    )
    return json.dumps(payload, ensure_ascii=False, indent=2)
