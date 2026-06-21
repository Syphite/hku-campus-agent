"""Heuristics for choosing where to fill table-backed form fields."""

from __future__ import annotations

from agent.application.cell_utils import is_empty_cell


def _parsed_cell_empty(value) -> bool:
    text = str(value or "").strip()
    return is_empty_cell(text)


def _row_cells_from_parsed(row) -> list:
    if isinstance(row, dict):
        return row.get("cells") or []
    return list(row or [])


def infer_table_fill_location(rows: list, row_index: int, col_index: int) -> str:
    """
    Infer fill direction for a table label at (row_index, col_index).

    Rules (table-only):
    1. Empty cell to the right → fill right
    2. Right cell occupied (or missing) and empty cell below → fill below
    3. Fallback → right
    """
    if row_index < 0 or row_index >= len(rows):
        return "right"

    row_cells = _row_cells_from_parsed(rows[row_index])
    has_right = col_index + 1 < len(row_cells)
    right_empty = has_right and _parsed_cell_empty(row_cells[col_index + 1])
    right_filled = has_right and not _parsed_cell_empty(row_cells[col_index + 1])

    below_empty_same_col = False
    below_any_empty = False
    if row_index + 1 < len(rows):
        below_cells = _row_cells_from_parsed(rows[row_index + 1])
        if col_index < len(below_cells):
            below_empty_same_col = _parsed_cell_empty(below_cells[col_index])
        below_any_empty = any(_parsed_cell_empty(c) for c in below_cells)

    if right_empty:
        return "right"
    if (right_filled or not has_right) and below_empty_same_col:
        return "below"
    if (right_filled or not has_right) and below_any_empty:
        return "below"
    if below_empty_same_col:
        return "below"
    return "right"


def resolve_docx_table_target(table, row_index: int, col_index: int, cells) -> object | None:
    """
    Pick the DOCX table cell to fill using the same right-then-below heuristic.
    Returns a cell object or None.
    """
    has_right = col_index + 1 < len(cells)
    if has_right and is_empty_cell(cells[col_index + 1].text):
        return cells[col_index + 1]

    right_occupied = has_right and not is_empty_cell(cells[col_index + 1].text)
    if row_index + 1 < len(table.rows):
        below_cells = _dedupe_docx_row(table.rows[row_index + 1])
        if col_index < len(below_cells) and is_empty_cell(below_cells[col_index].text):
            return below_cells[col_index]
        if right_occupied or not has_right:
            for cell in below_cells:
                if is_empty_cell(cell.text):
                    return cell

    if row_index + 1 < len(table.rows):
        below_cells = _dedupe_docx_row(table.rows[row_index + 1])
        for cell in below_cells:
            if is_empty_cell(cell.text):
                return cell

    if has_right:
        return cells[col_index + 1]
    return None


def _dedupe_docx_row(row):
    seen = set()
    cells = []
    for cell in row.cells:
        cell_id = id(cell._tc)
        if cell_id in seen:
            continue
        seen.add(cell_id)
        cells.append(cell)
    return cells


def resolve_description_target(table, data_row_index: int, *, is_description_row) -> object | None:
    """Find the fill cell for a list-item description row (table-only)."""
    desc_row_index = data_row_index + 1
    if desc_row_index >= len(table.rows):
        return None

    desc_cells = _dedupe_docx_row(table.rows[desc_row_index])
    if not is_description_row(desc_cells):
        return None

    if desc_row_index + 1 < len(table.rows):
        fill_cells = _dedupe_docx_row(table.rows[desc_row_index + 1])
        for cell in fill_cells:
            if is_empty_cell(cell.text):
                return cell

    for cell in reversed(desc_cells):
        text = str(cell.text or "").strip().lower()
        if "description" in text or "in 50 words" in text:
            continue
        if is_empty_cell(cell.text):
            return cell

    if desc_row_index + 1 < len(table.rows):
        fill_cells = _dedupe_docx_row(table.rows[desc_row_index + 1])
        return fill_cells[0] if fill_cells else None
    return None
