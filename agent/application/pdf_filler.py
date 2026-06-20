"""Anchor-based PDF form filling using PyMuPDF."""

import logging
import os
import shutil

from agent.application.cell_utils import normalize_text, texts_match

logger = logging.getLogger(__name__)


def _insert_near_anchor(page, anchor_label: str, value: str, fill_location: str) -> bool:
    if not value or not anchor_label:
        return False

    rects = page.search_for(anchor_label)
    if not rects:
        normalized_anchor = anchor_label.strip()
        for variant in (normalized_anchor, normalized_anchor.split(":")[0]):
            rects = page.search_for(variant)
            if rects:
                break
    if not rects:
        return False

    rect = rects[0]
    fill_location = (fill_location or "right").lower()
    x = rect.x1 + 8
    y = rect.y0 + 2
    if fill_location == "below":
        x = rect.x0
        y = rect.y1 + 4

    page.insert_text((x, y), str(value), fontsize=9)
    return True


def _fill_repeating_list_pdf(page, list_schema: dict, items: list[dict]) -> int:
    headers = list_schema.get("column_headers") or []
    if not headers:
        return 0

    anchor_rect = None
    for header in headers:
        rects = page.search_for(header)
        if rects:
            anchor_rect = rects[0]
            break
    if not anchor_rect:
        return 0

    item_fields = list_schema.get("item_fields") or {}
    max_rows = int(list_schema.get("max_rows") or len(items) or 5)
    row_height = 16
    filled = 0

    for index, item in enumerate(items[:max_rows]):
        y = anchor_rect.y1 + 8 + (index * row_height)
        x = anchor_rect.x0
        parts = []
        for logical_key in ("dates", "organization", "role", "hours", "description"):
            label = item_fields.get(logical_key, logical_key)
            value = item.get(logical_key, "")
            if value:
                parts.append(f"{label}: {value}")
        line = " | ".join(parts)
        if line:
            page.insert_text((x, y), line, fontsize=8)
            filled += 1
    return filled


def fill_pdf_form(original_path: str, filled_data: dict, schema: dict, output_path: str | None = None) -> str:
    import fitz

    if not os.path.exists(original_path):
        raise FileNotFoundError(original_path)

    target_path = output_path or original_path.replace(".pdf", "_filled.pdf")
    if target_path == original_path:
        target_path = original_path.replace(".pdf", "_filled.pdf")
    shutil.copy2(original_path, target_path)

    doc = fitz.open(target_path)
    page = doc[0]

    for field in schema.get("simple_fields", []):
        value = (filled_data.get("simple_fields") or {}).get(field.get("key"), "")
        _insert_near_anchor(page, field.get("anchor_label", ""), value, field.get("fill_location", "right"))

    for field in schema.get("booleans", []):
        raw_value = (filled_data.get("booleans") or {}).get(field.get("key"))
        if raw_value is None:
            continue
        value = "Yes" if raw_value else "No"
        _insert_near_anchor(page, field.get("anchor_label", ""), value, field.get("fill_location", "right"))

    for field in schema.get("long_text", []):
        value = (filled_data.get("long_text") or {}).get(field.get("key"), "")
        if value:
            _insert_near_anchor(page, field.get("anchor_label", ""), value, field.get("fill_location", "below"))

    for field in schema.get("repeating_lists", []):
        items = (filled_data.get("repeating_lists") or {}).get(field.get("key"), [])
        if items:
            _fill_repeating_list_pdf(page, field, items)

    doc.save(target_path)
    doc.close()
    logger.info("Filled PDF saved to %s", target_path)
    return target_path
