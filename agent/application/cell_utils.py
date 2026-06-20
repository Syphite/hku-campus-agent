"""Shared helpers for anchor matching and cell normalization."""

import re


def normalize_text(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def is_empty_cell(value: str) -> bool:
    text = str(value or "").strip()
    return not text or text == "[Empty]"


def texts_match(anchor: str, candidate: str) -> bool:
    anchor_norm = normalize_text(anchor)
    candidate_norm = normalize_text(candidate)
    if not anchor_norm or not candidate_norm:
        return False
    return anchor_norm in candidate_norm or candidate_norm in anchor_norm


def dedupe_row_cells(cell_texts: list[str]) -> list[str]:
    """Collapse python-docx merged-cell duplicates within a row."""
    deduped = []
    previous = object()
    for text in cell_texts:
        normalized = str(text or "").strip()
        if normalized == previous:
            continue
        deduped.append(normalized if normalized else "[Empty]")
        previous = normalized
    return deduped


def parse_llm_json(raw: str):
    import json

    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)
