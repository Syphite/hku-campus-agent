"""AI analysis and conversational data helpers for application forms."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from dotenv import load_dotenv
from openai import AzureOpenAI

from agent.application.cell_utils import normalize_text, parse_llm_json
from agent.application.profile_resolver import resolve_profile_field, truncate_to_words

load_dotenv()
logger = logging.getLogger(__name__)

PROMPT_DIR = os.path.join(os.path.dirname(__file__), "..", "prompts")
TABLE_BATCH_SIZE = 4
SCHEMA_MAX_TOKENS = 8000


def _load_prompt(name: str) -> str:
    with open(os.path.join(PROMPT_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _get_openai_client() -> AzureOpenAI:
    return AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version="2024-12-01-preview",
    )


def _deployment() -> str:
    return os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")


def _profile_value(profile: dict, profile_key: str) -> str:
    return resolve_profile_field(profile, {"profile_key": profile_key, "key": profile_key})


def _valid_paragraph_index(value, paragraph_count: int) -> bool:
    try:
        index = int(value)
    except (TypeError, ValueError):
        return False
    return 0 <= index < paragraph_count


def _field_has_location(field: dict, table_count: int, paragraph_count: int) -> bool:
    source = (field.get("source") or "table").lower()
    if source == "paragraph":
        return _valid_paragraph_index(field.get("paragraph_index"), paragraph_count)
    try:
        index = int(field.get("table_index", -1))
    except (TypeError, ValueError):
        return False
    return 0 <= index < table_count


def _validate_schema(schema: dict, form_payload: dict) -> dict:
    tables = form_payload.get("tables") or []
    paragraphs = form_payload.get("paragraphs") or []
    table_count = len(tables)
    paragraph_count = len(paragraphs)

    cleaned = {
        "simple_fields": [],
        "repeating_lists": [],
        "long_text": [],
        "booleans": [],
    }

    for field in schema.get("simple_fields", []) or []:
        if field.get("anchor_label") and field.get("key") and _field_has_location(field, table_count, paragraph_count):
            cleaned["simple_fields"].append(field)

    for field in schema.get("repeating_lists", []) or []:
        headers = field.get("column_headers") or []
        if field.get("key") and headers and _field_has_location(field, table_count, paragraph_count):
            if field.get("max_rows") is not None:
                try:
                    field["max_rows"] = int(field["max_rows"])
                except (TypeError, ValueError):
                    field.pop("max_rows", None)
            if not field.get("list_kind"):
                field["list_kind"] = infer_list_kind(field)
            cleaned["repeating_lists"].append(field)

    for field in schema.get("long_text", []) or []:
        if field.get("anchor_label") and field.get("key") and _field_has_location(field, table_count, paragraph_count):
            cleaned["long_text"].append(field)

    for field in schema.get("booleans", []) or []:
        if field.get("anchor_label") and field.get("key") and _field_has_location(field, table_count, paragraph_count):
            cleaned["booleans"].append(field)

    return cleaned


def _analyze_schema_batch(form_payload: dict, table_batch: list) -> dict:
    batch_payload = {
        **form_payload,
        "tables": table_batch,
        "analysis_note": f"Analyzing tables {table_batch[0]['table_index']} to {table_batch[-1]['table_index']} only.",
    }
    batch_json = json.dumps(batch_payload, ensure_ascii=False, indent=2)
    prompt = _load_prompt("form_schema_analysis.txt").format(form_json=batch_json)
    client = _get_openai_client()
    response = client.chat.completions.create(
        model=_deployment(),
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=SCHEMA_MAX_TOKENS,
        temperature=0.1,
    )
    parsed = parse_llm_json(response.choices[0].message.content or "{}")
    if not isinstance(parsed, dict):
        raise ValueError("Form schema analysis did not return a JSON object")
    return _validate_schema(parsed, form_payload)


def merge_table_schemas(partial_schemas: list[dict]) -> dict:
    merged = {
        "simple_fields": [],
        "repeating_lists": [],
        "long_text": [],
        "booleans": [],
    }
    seen = {category: set() for category in merged}

    for schema in partial_schemas or []:
        for category in merged:
            for field in schema.get(category, []) or []:
                key = field.get("key") or field.get("anchor_label")
                table_index = field.get("table_index", "")
                dedupe_key = (category, key, table_index, field.get("paragraph_index", ""))
                if dedupe_key in seen[category]:
                    continue
                seen[category].add(dedupe_key)
                merged[category].append(field)
    return merged


def count_fill_targets(schema: dict, free_fields: list | None = None) -> int:
    total = sum(len(schema.get(category, []) or []) for category in (
        "simple_fields", "repeating_lists", "long_text", "booleans"
    ))
    total += len(free_fields or [])
    return total


def analyze_form_schema_chunked(form_payload: dict) -> tuple[dict, list[str]]:
    """Analyze large forms in table batches; return merged schema and warnings."""
    tables = form_payload.get("tables") or []
    if not tables:
        return {"simple_fields": [], "repeating_lists": [], "long_text": [], "booleans": []}, []

    partial_schemas = []
    errors = []
    for start in range(0, len(tables), TABLE_BATCH_SIZE):
        batch = tables[start:start + TABLE_BATCH_SIZE]
        try:
            partial_schemas.append(_analyze_schema_batch(form_payload, batch))
        except Exception as exc:
            first = batch[0].get("table_index", start)
            last = batch[-1].get("table_index", start + len(batch) - 1)
            message = f"Tables {first}-{last}: {exc}"
            logger.warning("Schema batch failed: %s", message)
            errors.append(message)

    merged = merge_table_schemas(partial_schemas)
    if count_fill_targets(merged) == 0 and errors:
        raise ValueError("; ".join(errors))
    return merged, errors


def analyze_form_schema(table_json: str) -> dict:
    """Backward-compatible single-call wrapper."""
    form_payload = json.loads(table_json)
    schema, _errors = analyze_form_schema_chunked(form_payload)
    return schema


def _match_widget_name(question_text: str, widgets: list[dict]) -> str | None:
    question_norm = normalize_text(question_text)
    if not question_norm:
        return None
    for widget in widgets or []:
        name = str(widget.get("field_name") or "")
        name_norm = normalize_text(name.replace("_", " "))
        if not name_norm:
            continue
        if name_norm in question_norm or question_norm[:40] in name_norm:
            return name
        essay_keys = ("essay", "statement", "reason", "experience", "answer", "description")
        if any(key in name_norm for key in essay_keys) and any(key in question_norm for key in essay_keys):
            return name
    return None


def extract_free_form_fields(form_payload: dict) -> list[dict]:
    """Extract non-table essay/open fields from paragraphs and AcroForm widgets."""
    paragraphs = form_payload.get("paragraphs") or []
    widgets = form_payload.get("acroform_widgets") or []
    content_controls = form_payload.get("content_controls") or []

    paragraph_inventory = "\n".join(
        f"{item.get('paragraph_index', item.get('line_index', idx))}: {item.get('text', '')[:200]}"
        for idx, item in enumerate(paragraphs[:80])
    )
    widget_inventory = "\n".join(
        f"{widget.get('field_name', '')}: {widget.get('field_type', '')}"
        for widget in widgets[:40]
    ) or "(none)"

    raw_parts = [item.get("text", "") for item in paragraphs[:80]]
    raw_text = "\n".join(raw_parts)[:12000]

    if not raw_text.strip() and not widgets and not content_controls:
        return []

    try:
        prompt = _load_prompt("form_free_field_extract.txt").format(
            paragraph_inventory=paragraph_inventory or "(none)",
            widget_inventory=widget_inventory,
            raw_text=raw_text or "(none)",
        )
        client = _get_openai_client()
        response = client.chat.completions.create(
            model=_deployment(),
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=2000,
            temperature=0.1,
        )
        parsed = parse_llm_json(response.choices[0].message.content or "{}")
        candidates = parsed.get("free_fields", []) if isinstance(parsed, dict) else []
    except Exception as exc:
        logger.warning("Free-field extraction failed: %s", exc)
        candidates = []

    free_fields = []
    seen_keys = set()
    for item in candidates or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        question_text = str(item.get("question_text") or item.get("anchor_label") or "").strip()
        if not key or not question_text or key in seen_keys:
            continue
        seen_keys.add(key)

        field = {
            "key": key,
            "question_text": question_text,
            "anchor_label": str(item.get("anchor_label") or question_text[:80]).strip(),
            "source": "paragraph",
            "fill_strategy": item.get("fill_strategy") or "paragraph_append",
            "paragraph_index": item.get("paragraph_index"),
            "field_name": item.get("field_name"),
        }

        if widgets:
            matched = field.get("field_name") or _match_widget_name(question_text, widgets)
            if matched:
                field["field_name"] = matched
                field["source"] = "acroform"
                field["fill_strategy"] = "acroform_widget"

        if field.get("paragraph_index") is None and content_controls:
            anchor_norm = normalize_text(field["anchor_label"])
            for control in content_controls:
                label_norm = normalize_text(control.get("label", ""))
                if anchor_norm and label_norm and (anchor_norm in label_norm or label_norm in anchor_norm):
                    field["content_control_index"] = control.get("index")
                    field["source"] = "content_control"
                    break

        free_fields.append(field)

    return free_fields[:15]


LIST_KIND_KEYWORDS = {
    "education": ("school", "degree", "qualification", "tertiary", "secondary", "institution", "education", "university", "college"),
    "volunteer": ("volunteer", "service", "unit", "hours", "community service"),
    "award": ("award", "prize", "achievement", "competition", "honour", "honor", "dean's list"),
    "work": ("employment", "employer", "position", "work experience", "internship", "job"),
    "social_good": ("social good", "community", "engagement", "social service"),
}


def safe_max_rows(schema: dict | None) -> int:
    try:
        value = int((schema or {}).get("max_rows") or 5)
        return value if value > 0 else 5
    except (TypeError, ValueError):
        return 5


def infer_list_kind(list_schema: dict) -> str:
    label = normalize_text(str(list_schema.get("label") or list_schema.get("key") or ""))
    instructions = normalize_text(str(list_schema.get("instructions") or ""))
    headers = " ".join(normalize_text(str(header)) for header in (list_schema.get("column_headers") or []))
    blob = f"{label} {instructions} {headers}"
    scores = {}
    for kind, keywords in LIST_KIND_KEYWORDS.items():
        scores[kind] = sum(1 for keyword in keywords if keyword in blob)
    best_kind, best_score = max(scores.items(), key=lambda item: item[1])
    return best_kind if best_score > 0 else "generic"


def list_entry_field_keys(list_schema: dict) -> list[str]:
    item_fields = list_schema.get("item_fields") or {}
    if isinstance(item_fields, dict) and item_fields:
        return list(item_fields.keys())
    kind = list_schema.get("list_kind") or infer_list_kind(list_schema)
    if kind == "education":
        return ["institution", "qualification", "dates", "description"]
    if kind == "award":
        return ["dates", "role", "organization", "description"]
    if kind == "work":
        return ["dates", "organization", "role", "employment_type", "description"]
    return ["organization", "role", "dates", "hours", "description"]


def build_list_section_prompt(gap: dict) -> str:
    schema = gap.get("schema") or {}
    label = gap.get("label") or gap.get("key", "Section").replace("_", " ").title()
    max_rows = safe_max_rows(schema)
    kind = schema.get("list_kind") or infer_list_kind(schema)
    headers = schema.get("column_headers") or []
    header_hint = ", ".join(str(header) for header in headers[:6] if header)

    if kind == "education":
        instructions = (schema.get("instructions") or "").strip()
        if not instructions:
            instructions = (
                "Please provide details of your PREVIOUS tertiary and secondary education, "
                "starting with the most recent one."
            )
        fields_hint = header_hint or "institution, qualification, dates"
        return (
            f"**{label}**\n\n{instructions}\n\n"
            f"Paste all entries in one message (up to {max_rows}). "
            f"Include {fields_hint} for each."
        )

    field_hints = {
        "volunteer": "organization, role, dates, and hours",
        "award": "award name, organization, dates, and brief description",
        "work": "employer, role, dates, and description",
        "social_good": "organization, activity, dates, and impact",
        "generic": header_hint or "the fields shown in the form for each entry",
    }
    hint = field_hints.get(kind, field_hints["generic"])
    return (
        f"**{label}** — paste all entries in one message (up to {max_rows}). "
        f"Include {hint} for each."
    )


def extract_profile_activity_blobs(profile: dict) -> list[str]:
    blobs: list[str] = []
    for item in profile.get("activities") or []:
        text = str(item or "").strip()
        if not text:
            continue
        if any(sep in text for sep in ("\n", ";")):
            for part in text.replace(";", "\n").split("\n"):
                cleaned = part.strip(" •-\t")
                if cleaned:
                    blobs.append(cleaned)
        elif "," in text and len(text) > 40:
            for part in text.split(","):
                cleaned = part.strip()
                if cleaned:
                    blobs.append(cleaned)
        else:
            blobs.append(text)

    cv_text = str(profile.get("cv_text") or "").strip()
    if cv_text:
        for line in cv_text.split("\n"):
            cleaned = line.strip(" •-\t")
            if len(cleaned) >= 8:
                blobs.append(cleaned)

    seen = set()
    unique = []
    for blob in blobs:
        key = blob.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(blob)
    return unique[:20]


def _compact_profile_for_mapping(profile: dict) -> dict:
    academic = profile.get("academic") or {}
    return {
        "faculty": academic.get("faculty") or profile.get("faculty", ""),
        "programme": academic.get("programme") or profile.get("programme", ""),
        "year_of_study": academic.get("year_of_study") or profile.get("year", ""),
        "interests": profile.get("interests") or [],
        "activities": extract_profile_activity_blobs(profile),
    }


def suggest_profile_entries_for_gap(gap: dict, profile: dict) -> list[dict]:
    activity_blobs = extract_profile_activity_blobs(profile)
    if not activity_blobs or gap.get("type") != "repeating_list":
        return []

    schema = gap.get("schema") or {}
    section_payload = {
        "key": gap.get("key"),
        "label": gap.get("label"),
        "list_kind": schema.get("list_kind") or infer_list_kind(schema),
        "column_headers": schema.get("column_headers") or [],
        "item_fields": schema.get("item_fields") or {},
        "instructions": schema.get("instructions") or "",
    }
    field_keys = list_entry_field_keys(schema)

    prompt = _load_prompt("form_profile_section_map.txt").format(
        student_profile=json.dumps(_compact_profile_for_mapping(profile), ensure_ascii=False, indent=2),
        section_schema=json.dumps(section_payload, ensure_ascii=False, indent=2),
        field_keys=", ".join(field_keys),
    )
    try:
        client = _get_openai_client()
        response = client.chat.completions.create(
            model=_deployment(),
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=1500,
            temperature=0.1,
        )
        parsed = parse_llm_json(response.choices[0].message.content or "{}")
        if not isinstance(parsed, dict):
            return []
        suggestions = []
        for item in parsed.get("suggestions", []) or []:
            if not isinstance(item, dict):
                continue
            mapped = item.get("mapped_fields") or {}
            if not isinstance(mapped, dict):
                continue
            entry = {key: str(mapped.get(key) or "").strip() for key in field_keys}
            if not any(entry.values()):
                continue
            desc_limit = int(schema.get("description_target_words") or 0)
            if desc_limit and entry.get("description"):
                entry["description"] = truncate_to_words(entry["description"], desc_limit)
            entry["_source_text"] = str(item.get("source_text") or "").strip()
            entry["_confidence"] = str(item.get("confidence") or "").strip()
            suggestions.append(entry)
            if len(suggestions) >= safe_max_rows(schema):
                break
        return suggestions
    except Exception as exc:
        logger.warning("Profile section mapping failed for %s: %s", gap.get("key"), exc)
        return []


def build_profile_suggestions_for_gaps(gaps: list, profile: dict) -> dict:
    suggestions = {}
    for gap in consolidate_collection_gaps(gaps):
        section_id = gap.get("section_id") or gap.get("key")
        if not section_id:
            continue
        entries = suggest_profile_entries_for_gap(gap, profile)
        if entries:
            suggestions[section_id] = entries
    return suggestions


def build_profile_suggestions_overview(gaps: list, profile: dict, profile_suggestions: dict | None = None) -> str:
    profile_suggestions = profile_suggestions or build_profile_suggestions_for_gaps(gaps, profile)
    if not profile_suggestions:
        return ""

    lines = ["**From your profile, I may be able to pre-fill:**"]
    for gap in consolidate_collection_gaps(gaps):
        section_id = gap.get("section_id") or gap.get("key")
        entries = profile_suggestions.get(section_id) or []
        if not entries:
            continue
        label = gap.get("label") or gap.get("key", "Section").replace("_", " ").title()
        preview_parts = []
        seen_previews = set()
        for entry in entries[:3]:
            preview = entry.get("_source_text") or ", ".join(
                str(v) for k, v in entry.items() if v and not str(k).startswith("_")
            )
            preview = preview.strip()
            if not preview or preview.lower() in seen_previews:
                continue
            seen_previews.add(preview.lower())
            preview_parts.append(preview[:120] + ("..." if len(preview) > 120 else ""))
        if preview_parts:
            lines.append(f"- **{label}**: {'; '.join(preview_parts)}")
    lines.append("")
    lines.append("I'll ask you to confirm before using these in each matching section.")
    return "\n".join(lines)


def build_filled_data(schema: dict, profile: dict, free_fields: list | None = None) -> dict:
    simple_values = {}
    for field in schema.get("simple_fields", []):
        key = field.get("key")
        if not key:
            continue
        value = resolve_profile_field(profile, field)
        if value:
            simple_values[key] = value

    repeating_values = {}
    structured_lists = profile.get("structured_lists") or {}
    for field in schema.get("repeating_lists", []):
        key = field.get("key")
        if not key:
            continue
        repeating_values[key] = list(structured_lists.get(key) or [])

    long_text_values = {}
    for field in schema.get("long_text", []):
        key = field.get("key")
        if key:
            long_text_values[key] = ""

    boolean_values = {}
    for field in schema.get("booleans", []):
        key = field.get("key")
        if not key:
            continue
        anchor = normalize_text(field.get("anchor_label", ""))
        if "local" in anchor:
            boolean_values[key] = profile.get("academic", {}).get("nationality", {}).get("local_status") == "local"
        else:
            boolean_values[key] = None

    free_values = {}
    for field in free_fields or []:
        key = field.get("key")
        if key:
            free_values[key] = ""

    return {
        "simple_fields": simple_values,
        "repeating_lists": repeating_values,
        "long_text": long_text_values,
        "booleans": boolean_values,
        "free_fields": free_values,
    }


def detect_gaps(schema: dict, filled_data: dict, profile: dict, free_field_defs: list | None = None) -> list[dict]:
    gaps = []

    for field in schema.get("repeating_lists", []):
        key = field.get("key")
        items = (filled_data.get("repeating_lists") or {}).get(key) or []
        if items:
            continue
        list_schema = dict(field)
        if not list_schema.get("list_kind"):
            list_schema["list_kind"] = infer_list_kind(list_schema)
        gap = {
            "type": "repeating_list",
            "key": key,
            "label": field.get("label") or key.replace("_", " ").title(),
            "schema": list_schema,
        }
        gap["prompt"] = build_list_section_prompt(gap)
        gaps.append(gap)

    for field in schema.get("long_text", []):
        key = field.get("key")
        current = (filled_data.get("long_text") or {}).get(key) or ""
        if current.strip():
            continue
        label = field.get("anchor_label") or key.replace("_", " ").title()
        if is_declaration_field_label(label):
            continue
        gaps.append({
            "type": "long_text",
            "key": key,
            "label": label,
            "schema": field,
            "prompt": (
                f"**{label}** — paste your answer, say **draft** for an AI draft from your profile and activities, "
                "or **skip** to leave blank."
            ),
        })

    for field in free_field_defs or []:
        key = field.get("key")
        current = (filled_data.get("free_fields") or {}).get(key) or ""
        if current.strip():
            continue
        label = field.get("question_text") or key.replace("_", " ").title()
        if is_declaration_field_label(label):
            continue
        gaps.append({
            "type": "free_field",
            "key": key,
            "label": label,
            "schema": field,
            "prompt": (
                f"**{label}** — paste your answer, say **draft** for an AI draft from your profile and activities, "
                "or **skip** to leave blank."
            ),
        })

    if not gaps and not profile.get("activities"):
        logger.info("No structured list gaps detected")

    return consolidate_collection_gaps(gaps)


COLLECTION_GAP_ORDER = {"repeating_list": 0, "free_field": 1, "long_text": 2}


def sort_collection_gaps(gaps: list) -> list:
    items = [gap for gap in gaps or [] if gap.get("type") in COLLECTION_GAP_ORDER]
    return sorted(items, key=lambda gap: COLLECTION_GAP_ORDER[gap["type"]])


def gap_section_id(gap: dict) -> str:
    """Stable id for merging duplicate schema rows into one collection step."""
    gap_type = gap.get("type", "")
    label = normalize_text(gap.get("label") or gap.get("key") or "section")
    if gap_type == "repeating_list":
        schema = gap.get("schema") or {}
        kind = schema.get("list_kind") or infer_list_kind(schema)
        return f"{gap_type}:{kind}:{label}"
    return f"{gap_type}:{label}"


def consolidate_collection_gaps(gaps: list) -> list:
    """
    Merge duplicate gaps (same label/kind from multiple tables) into one collection step.
    Preserves all schema variants for form filling via keys[] and schema_variants[].
    """
    consolidated: dict[str, dict] = {}
    order: list[str] = []

    for gap in sort_collection_gaps(gaps):
        sid = gap_section_id(gap)
        if sid not in consolidated:
            entry = dict(gap)
            entry["section_id"] = sid
            entry["keys"] = [gap["key"]] if gap.get("key") else []
            entry["schema_variants"] = [dict(gap.get("schema") or {})] if gap.get("schema") else []
            consolidated[sid] = entry
            order.append(sid)
            continue

        existing = consolidated[sid]
        key = gap.get("key")
        if key and key not in existing["keys"]:
            existing["keys"].append(key)
        schema = gap.get("schema")
        if schema:
            existing["schema_variants"].append(dict(schema))
        if gap.get("type") == "repeating_list":
            existing["schema"] = dict(existing.get("schema") or {})
            existing["schema"]["max_rows"] = max(
                safe_max_rows(existing.get("schema")),
                safe_max_rows(schema or {}),
            )
            existing["prompt"] = build_list_section_prompt(existing)

    return [consolidated[sid] for sid in order]


def gap_data_keys(gap: dict) -> list[str]:
    """All storage keys tied to this consolidated section."""
    keys = list(gap.get("keys") or [])
    primary = gap.get("key")
    if primary and primary not in keys:
        keys.insert(0, primary)
    return [k for k in keys if k]


def is_declaration_field_label(label: str) -> bool:
    """Fields the student should complete directly in the DOCX (Yes/No follow-ups, declarations)."""
    normalized = normalize_text(label or "")
    if not normalized:
        return False
    if "declaration" in normalized:
        return True
    if "questions above" in normalized:
        return True
    if "provide details" in normalized and "yes" in normalized:
        return True
    if "answered" in normalized and "yes" in normalized:
        return True
    return False


def build_section_prompt(gap: dict) -> str:
    if not gap:
        return "Please share the information for the next section."
    if gap.get("prompt"):
        return gap["prompt"]
    gap_type = gap.get("type")
    if gap_type == "repeating_list":
        return build_list_section_prompt(gap)
    label = gap.get("label") or gap.get("key", "Section").replace("_", " ").title()
    return (
        f"**{label}** — paste your answer, say **draft** for an AI draft from your profile and activities, "
        "or **skip** to leave blank."
    )


DECLARATIONS_DOCX_NOTE = (
    "**Complete in the form document:** Declaration sections and any "
    "\"If you answered Yes…\" follow-ups should be filled in directly in the downloaded DOCX."
)


def build_gap_overview(schema: dict, filled_data: dict, gaps: list) -> str:
    lines = ["I analyzed your form.", ""]

    simple_fields = filled_data.get("simple_fields") or {}
    filled_simple = [key.replace("_", " ").title() for key, value in simple_fields.items() if value]
    if filled_simple:
        lines.append("**Already filled from your profile:**")
        for label in filled_simple[:12]:
            lines.append(f"- {label}")
        lines.append("")

    lines.append(DECLARATIONS_DOCX_NOTE)
    lines.append("")

    collection_gaps = consolidate_collection_gaps(gaps)
    if collection_gaps:
        lines.append("**Still needed:**")
        for index, gap in enumerate(collection_gaps, start=1):
            label = gap.get("label") or gap.get("key", "Section").replace("_", " ").title()
            gap_type = gap.get("type")
            if gap_type == "repeating_list":
                max_rows = safe_max_rows(gap.get("schema"))
                lines.append(f"{index}. {label} — up to {max_rows} entries (send all in one message)")
            elif gap_type in ("long_text", "free_field"):
                short_label = label if len(label) <= 100 else f"{label[:97]}..."
                lines.append(f"{index}. {short_label} — essay/open answer (**draft** or **skip** ok)")
            else:
                lines.append(f"{index}. {label}")
        lines.append("")
        lines.append(
            "Say **skip [section]** to skip, **review** to finish early, or **cancel application** to stop."
        )
    else:
        lines.append("Your profile covers the required fields. Review the summary next.")

    return "\n".join(lines)


def _normalize_batch_entry(item: dict, field_keys: list[str], list_schema: dict | None = None) -> dict:
    normalized = {key: str(item.get(key) or "").strip() for key in field_keys}
    if "institution" in field_keys and not normalized.get("institution"):
        normalized["institution"] = str(item.get("organization") or item.get("school") or "").strip()
    if "organization" in field_keys and not normalized.get("organization"):
        normalized["organization"] = str(item.get("institution") or item.get("school") or "").strip()
    if "qualification" in field_keys and not normalized.get("qualification"):
        normalized["qualification"] = str(item.get("role") or item.get("degree") or "").strip()
    if "role" in field_keys and not normalized.get("role"):
        normalized["role"] = str(item.get("qualification") or item.get("position") or "").strip()
    if "dates" in field_keys and not normalized.get("dates"):
        normalized["dates"] = str(item.get("period") or item.get("date") or "").strip()
    if "description" in field_keys and not normalized.get("description"):
        normalized["description"] = str(item.get("details") or item.get("summary") or "").strip()
    if "hours" in field_keys and not normalized.get("hours"):
        normalized["hours"] = str(item.get("hours") or "").strip()
    if "employment_type" in field_keys and not normalized.get("employment_type"):
        normalized["employment_type"] = str(
            item.get("employment_type") or item.get("hours") or item.get("type") or ""
        ).strip()
    desc_limit = int((list_schema or {}).get("description_target_words") or 0) if list_schema else 0
    if desc_limit and normalized.get("description"):
        normalized["description"] = truncate_to_words(normalized["description"], desc_limit)
    return normalized


def parse_list_entries_batch(user_text: str, list_schema: dict, max_rows: int | None = None) -> list[dict]:
    max_rows = max_rows if max_rows is not None else safe_max_rows(list_schema)
    field_keys = list_entry_field_keys(list_schema)
    example_entry = {key: "..." for key in field_keys}
    prompt = _load_prompt("form_list_batch_parse.txt").format(
        list_schema=json.dumps(list_schema, ensure_ascii=False, indent=2),
        user_text=user_text,
        max_rows=max_rows,
        field_keys=", ".join(field_keys),
        example_entry=json.dumps(example_entry, ensure_ascii=False),
    )
    client = _get_openai_client()
    response = client.chat.completions.create(
        model=_deployment(),
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=2000,
        temperature=0.1,
    )
    parsed = parse_llm_json(response.choices[0].message.content or "{}")
    if not isinstance(parsed, dict):
        return []

    entries = []
    for item in parsed.get("entries", []) or []:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_batch_entry(item, field_keys, list_schema)
        if any(normalized.values()):
            entries.append(normalized)
        if len(entries) >= max_rows:
            break
    return entries


def parse_application_collection(user_text: str, current_gap: dict, profile: dict, state: dict) -> dict:
    section_label = current_gap.get("label") or current_gap.get("key", "current section")
    section_type = current_gap.get("type", "unknown")
    current_section = json.dumps({
        "type": section_type,
        "label": section_label,
        "key": current_gap.get("key"),
        "prompt": build_section_prompt(current_gap),
    }, ensure_ascii=False, indent=2)

    prompt = _load_prompt("form_collection_router.txt").format(
        current_section=current_section,
        user_text=user_text,
    )
    try:
        client = _get_openai_client()
        response = client.chat.completions.create(
            model=_deployment(),
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=800,
            temperature=0.1,
        )
        parsed = parse_llm_json(response.choices[0].message.content or "{}")
        if isinstance(parsed, dict):
            return parsed
    except Exception as exc:
        logger.warning("Application collection router failed: %s", exc)

    lowered = (user_text or "").strip().lower()
    if any(term in lowered for term in ("skip", "next", "move on", "pass", "no more", "don't have", "dont have", "nothing to add")):
        return {
            "intent": "skip_section",
            "extracted_data": {},
            "agent_response": f"Okay, skipping **{section_label}**.",
        }
    if any(term in lowered for term in ("draft", "write it for me", "generate")):
        if section_type in ("long_text", "free_field"):
            return {
                "intent": "draft_section",
                "extracted_data": {},
                "agent_response": f"I'll draft **{section_label}** from your profile.",
            }
    return {
        "intent": "fill_section",
        "extracted_data": {"answer_text": user_text},
        "agent_response": "",
    }


def parse_list_entry(user_text: str, list_schema: dict) -> dict:
    prompt = _load_prompt("form_list_entry_parse.txt").format(
        list_schema=json.dumps(list_schema, ensure_ascii=False, indent=2),
        user_text=user_text,
    )
    client = _get_openai_client()
    response = client.chat.completions.create(
        model=_deployment(),
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=800,
        temperature=0.1,
    )
    parsed = parse_llm_json(response.choices[0].message.content or "{}")
    if not isinstance(parsed, dict):
        return {}
    return {
        "organization": str(parsed.get("organization") or "").strip(),
        "role": str(parsed.get("role") or "").strip(),
        "dates": str(parsed.get("dates") or "").strip(),
        "hours": str(parsed.get("hours") or "").strip(),
        "description": str(parsed.get("description") or "").strip(),
    }


def draft_long_text(field_schema: dict, profile: dict, collected_lists: dict) -> str:
    target_words = int(field_schema.get("target_words") or 800)
    prompt = _load_prompt("form_long_text_draft.txt").format(
        target_words=target_words,
        field_schema=json.dumps(field_schema, ensure_ascii=False, indent=2),
        profile_json=json.dumps(profile, ensure_ascii=False, indent=2),
        collected_lists_json=json.dumps(collected_lists, ensure_ascii=False, indent=2),
    )
    client = _get_openai_client()
    response = client.chat.completions.create(
        model=_deployment(),
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=2500,
        temperature=0.35,
    )
    parsed = parse_llm_json(response.choices[0].message.content or "{}")
    if isinstance(parsed, dict):
        return str(parsed.get("text") or "").strip()
    return ""


def merge_filled_data(
    base: dict,
    pending_lists: dict,
    long_text_drafts: dict,
    pending_free_fields: dict | None = None,
) -> dict:
    merged = {
        "simple_fields": dict(base.get("simple_fields") or {}),
        "repeating_lists": dict(base.get("repeating_lists") or {}),
        "long_text": dict(base.get("long_text") or {}),
        "booleans": dict(base.get("booleans") or {}),
        "free_fields": dict(base.get("free_fields") or {}),
    }
    for key, items in (pending_lists or {}).items():
        merged["repeating_lists"][key] = list(items)
    for key, text in (long_text_drafts or {}).items():
        merged["long_text"][key] = text
    for key, text in (pending_free_fields or {}).items():
        merged["free_fields"][key] = text
    return merged
