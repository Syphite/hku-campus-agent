"""Local smoke test for table-aware application form filling."""

import json
import os
import sys

from agent.application.docx_parser import extract_form_schema
from agent.application.form_ai import build_filled_data, detect_gaps, merge_filled_data
from agent.application.docx_filler import fill_docx_form


MOCK_PROFILE = {
    "name": "Alex Chan",
    "email": "alex.chan@connect.hku.hk",
    "phone": "91234567",
    "student_id": "3031234567",
    "academic": {
        "faculty": "Engineering",
        "programme": "Bachelor of Engineering in Computer Science",
        "year_of_study": 1,
        "gpa": 3.7,
        "level": "undergraduate",
        "nationality": {"local_status": "local", "country_of_origin": "Hong Kong"},
    },
    "interests": ["AI", "robotics"],
    "activities": ["HKU Robotics Team"],
}


MOCK_SCHEMA = {
    "simple_fields": [
        {
            "key": "name_en",
            "anchor_label": "Name in English",
            "fill_location": "right",
            "table_index": 0,
            "profile_key": "name",
        }
    ],
    "repeating_lists": [
        {
            "key": "volunteer_exp",
            "label": "Volunteer Experience",
            "table_index": 0,
            "max_rows": 5,
            "column_headers": ["Period", "Name of the Unit", "Role"],
            "description_row": True,
            "item_fields": {
                "dates": "Period",
                "organization": "Name of the Unit",
                "role": "Role",
                "description": "Description",
            },
        }
    ],
    "long_text": [],
    "booleans": [],
}


def main() -> int:
    fixture = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "tests",
        "fixtures",
        "DHCFS_Application_Form_2627.docx",
    )
    fixture = os.path.abspath(fixture)
    if not os.path.exists(fixture):
        print(f"Fixture not found: {fixture}")
        return 1

    form_json = extract_form_schema(fixture)
    payload = json.loads(form_json)
    print(f"Parsed {len(payload.get('tables', []))} tables from {fixture}")

    if os.environ.get("AZURE_OPENAI_ENDPOINT"):
        from agent.application.form_ai import analyze_form_schema

        schema = analyze_form_schema(form_json)
        print("AI schema keys:", list(schema.keys()))
    else:
        schema = MOCK_SCHEMA
        print("Azure OpenAI not configured; using mock schema")

    filled_data = build_filled_data(schema, MOCK_PROFILE)
    filled_data["repeating_lists"]["volunteer_exp"] = [
        {
            "dates": "Sep 2025 - May 2026",
            "organization": "Code4HK",
            "role": "Volunteer Tutor",
            "hours": "40",
            "description": "Taught coding basics to secondary school students.",
        }
    ]
    gaps = detect_gaps(schema, filled_data, MOCK_PROFILE)
    print(f"Remaining gaps: {len(gaps)}")

    output_path = "/tmp/smoke_filled_application.docx"
    merged = merge_filled_data(filled_data, filled_data.get("repeating_lists", {}), {})
    result_path = fill_docx_form(fixture, merged, schema, output_path)
    print(f"Filled form written to: {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
