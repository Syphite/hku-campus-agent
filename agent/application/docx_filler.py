"""Anchor-based DOCX form filling."""

import logging
import os
import shutil

from agent.application.cell_utils import is_empty_cell, normalize_text, texts_match

logger = logging.getLogger(__name__)


def _set_cell_text(cell, value: str) -> None:
    text = str(value or "").strip()
    if not text:
        return
    cell.text = text


def _row_cells(table, row_index: int):
    row = table.rows[row_index]
    seen = set()
    cells = []
    for cell in row.cells:
        cell_id = id(cell._tc)
        if cell_id in seen:
            continue
        seen.add(cell_id)
        cells.append(cell)
    return cells


def _find_anchor(table, anchor_label: str):
    target = normalize_text(anchor_label)
    for row_index, row in enumerate(table.rows):
        cells = _row_cells(table, row_index)
        for col_index, cell in enumerate(cells):
            if texts_match(anchor_label, cell.text):
                return row_index, col_index, cell, cells
    return None


def _fill_at_location(table, row_index: int, col_index: int, cells, fill_location: str, value: str) -> bool:
    if not value:
        return False

    fill_location = (fill_location or "right").lower()
    if fill_location == "below":
        if row_index + 1 >= len(table.rows):
            return False
        below_cells = _row_cells(table, row_index + 1)
        target = below_cells[min(col_index, len(below_cells) - 1)]
        _set_cell_text(target, value)
        return True

    if fill_location in ("right", "table_row"):
        if col_index + 1 < len(cells):
            _set_cell_text(cells[col_index + 1], value)
            return True
        if row_index + 1 < len(table.rows):
            below_cells = _row_cells(table, row_index + 1)
            _set_cell_text(below_cells[0], value)
            return True
    return False


def _find_header_row(table, headers: list[str]):
    normalized_headers = [normalize_text(header) for header in headers if header]
    for row_index, row in enumerate(table.rows):
        cells = _row_cells(table, row_index)
        row_texts = [normalize_text(cell.text) for cell in cells]
        if all(any(header in text or text in header for text in row_texts) for header in normalized_headers):
            return row_index, cells
    return None


def _header_column_map(cells, headers: list[str], item_fields: dict) -> dict:
    mapping = {}
    row_texts = [normalize_text(cell.text) for cell in cells]
    for logical_key, header_label in (item_fields or {}).items():
        header_norm = normalize_text(header_label)
        for col_index, text in enumerate(row_texts):
            if texts_match(header_label, text) or header_norm in text or text in header_norm:
                mapping[logical_key] = col_index
                break
    return mapping


def _next_empty_data_row(table, start_row: int, column_map: dict, max_rows: int):
    for offset in range(1, max_rows + 1):
        row_index = start_row + offset
        if row_index >= len(table.rows):
            return None
        cells = _row_cells(table, row_index)
        values = []
        for col_index in column_map.values():
            if col_index < len(cells):
                values.append(cells[col_index].text)
        if all(is_empty_cell(value) for value in values):
            return row_index, cells
    return None


def _fill_repeating_list(table, list_schema: dict, items: list[dict]) -> int:
    headers = list_schema.get("column_headers") or []
    item_fields = list_schema.get("item_fields") or {}
    max_rows = int(list_schema.get("max_rows") or len(items) or 5)
    header_match = _find_header_row(table, headers)
    if not header_match:
        logger.warning("Could not find header row for repeating list %s", list_schema.get("key"))
        return 0

    header_row_index, header_cells = header_match
    column_map = _header_column_map(header_cells, headers, item_fields)
    if not column_map:
        logger.warning("Could not map columns for repeating list %s", list_schema.get("key"))
        return 0

    filled = 0
    for item in items[:max_rows]:
        target = _next_empty_data_row(table, header_row_index, column_map, max_rows)
        if not target:
            break
        row_index, cells = target
        for logical_key, col_index in column_map.items():
            if col_index < len(cells):
                _set_cell_text(cells[col_index], item.get(logical_key, ""))
        if list_schema.get("description_row") and row_index + 1 < len(table.rows):
            description = item.get("description") or item.get("hours") or ""
            if description:
                below_cells = _row_cells(table, row_index + 1)
                _set_cell_text(below_cells[0], description)
        filled += 1
    return filled


def fill_docx_form(original_path: str, filled_data: dict, schema: dict, output_path: str | None = None) -> str:
    from docx import Document

    if not os.path.exists(original_path):
        raise FileNotFoundError(original_path)

    target_path = output_path or original_path.replace(".docx", "_filled.docx")
    if target_path == original_path:
        target_path = original_path.replace(".docx", "_filled.docx")
    shutil.copy2(original_path, target_path)

    doc = Document(target_path)
    tables = doc.tables

    for field in schema.get("simple_fields", []):
        table_index = int(field.get("table_index", 0))
        if table_index >= len(tables):
            continue
        anchor = _find_anchor(tables[table_index], field.get("anchor_label", ""))
        if not anchor:
            continue
        row_index, col_index, _, cells = anchor
        value = (filled_data.get("simple_fields") or {}).get(field.get("key"), "")
        _fill_at_location(
            tables[table_index],
            row_index,
            col_index,
            cells,
            field.get("fill_location", "right"),
            value,
        )

    for field in schema.get("booleans", []):
        table_index = int(field.get("table_index", 0))
        if table_index >= len(tables):
            continue
        anchor = _find_anchor(tables[table_index], field.get("anchor_label", ""))
        if not anchor:
            continue
        row_index, col_index, _, cells = anchor
        raw_value = (filled_data.get("booleans") or {}).get(field.get("key"))
        if raw_value is None:
            continue
        value = "Yes" if raw_value else "No"
        _fill_at_location(
            tables[table_index],
            row_index,
            col_index,
            cells,
            field.get("fill_location", "right"),
            value,
        )

    for field in schema.get("long_text", []):
        table_index = int(field.get("table_index", 0))
        value = (filled_data.get("long_text") or {}).get(field.get("key"), "")
        if not value:
            continue
        if table_index < len(tables):
            anchor = _find_anchor(tables[table_index], field.get("anchor_label", ""))
            if anchor:
                row_index, col_index, _, cells = anchor
                _fill_at_location(
                    tables[table_index],
                    row_index,
                    col_index,
                    cells,
                    field.get("fill_location", "below"),
                    value,
                )
                continue
        for paragraph in doc.paragraphs:
            if texts_match(field.get("anchor_label", ""), paragraph.text):
                paragraph.text = f"{paragraph.text}\n{value}".strip()
                break

    for field in schema.get("repeating_lists", []):
        table_index = int(field.get("table_index", 0))
        if table_index >= len(tables):
            continue
        items = (filled_data.get("repeating_lists") or {}).get(field.get("key"), [])
        if items:
            _fill_repeating_list(tables[table_index], field, items)

    doc.save(target_path)
    logger.info("Filled DOCX saved to %s", target_path)
    return target_path
