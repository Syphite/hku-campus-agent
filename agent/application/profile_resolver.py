"""Map student profile data to scholarship form field values."""

from __future__ import annotations

import re
from datetime import datetime

from agent.application.cell_utils import normalize_text

HKU_FULL_NAME = "The University of Hong Kong"

PROGRAMME_EXPANSIONS = {
    "beng(cs)": "Bachelor of Engineering in Computer Science",
    "beng (cs)": "Bachelor of Engineering in Computer Science",
    "bengcse": "Bachelor of Engineering in Computer Science",
    "bsc(cs)": "Bachelor of Science in Computer Science",
    "bba": "Bachelor of Business Administration",
    "ba": "Bachelor of Arts",
    "bsc": "Bachelor of Science",
    "beng": "Bachelor of Engineering",
    "mbbs": "Bachelor of Medicine and Bachelor of Surgery",
    "llb": "Bachelor of Laws",
}


def truncate_to_words(text: str, max_words: int) -> str:
    words = re.findall(r"\S+", str(text or "").strip())
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words])


def expand_programme_name(programme: str) -> str:
    raw = str(programme or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if any(term in lowered for term in ("bachelor", "master", "doctor", "diploma")):
        return raw
    expanded = PROGRAMME_EXPANSIONS.get(lowered)
    if expanded:
        return expanded
    return raw


def _format_graduation_mm_yy(year_value) -> str:
    try:
        year = int(year_value)
    except (TypeError, ValueError):
        return ""
    if year < 100:
        year += 2000
    return f"06/{str(year)[-2:]}"


def _format_dob_dd_mm_yy(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%d/%m/%y")
        except ValueError:
            continue
    return raw


def _yes_no_permanent_resident(local_status: str) -> str:
    status = str(local_status or "").strip().lower()
    if status in ("local", "yes", "true", "permanent"):
        return "Yes"
    if status in ("non-local", "nonlocal", "no", "false"):
        return "No"
    return ""


def _university_email(profile: dict) -> str:
    email = str(profile.get("email") or "").strip()
    if email.endswith("@connect.hku.hk"):
        return email
    uni = str(profile.get("university_email") or "").strip()
    if uni:
        return uni
    student_id = str(profile.get("student_number") or profile.get("hku_student_id") or "").strip()
    if student_id and student_id.isdigit():
        return f"{student_id}@connect.hku.hk"
    return email if "@connect.hku.hk" in email else ""


def _personal_email(profile: dict) -> str:
    personal = str(profile.get("personal_email") or "").strip()
    if personal:
        return personal
    email = str(profile.get("email") or "").strip()
    if email and not email.endswith("@connect.hku.hk"):
        return email
    return personal


def _profile_flat(profile: dict) -> dict[str, str]:
    academic = profile.get("academic") or {}
    nationality = academic.get("nationality") or {}
    contact = profile.get("contact") or {}
    year = academic.get("year_of_study", "")
    year_label = ""
    if year:
        if str(year).lower() == "postgraduate":
            year_label = "Postgraduate"
        else:
            try:
                year_label = f"Year {int(year)}"
            except (TypeError, ValueError):
                year_label = str(year)

    return {
        "name": str(profile.get("name") or ""),
        "name_en": str(profile.get("name") or ""),
        "name_in_english": str(profile.get("name") or ""),
        "chinese_name": str(profile.get("chinese_name") or profile.get("name_zh") or ""),
        "preferred_name": str(profile.get("preferred_name") or profile.get("name") or ""),
        "gender": str(profile.get("gender") or ""),
        "university": HKU_FULL_NAME,
        "student_id": str(profile.get("student_number") or profile.get("hku_student_id") or profile.get("student_id") or ""),
        "student_number": str(profile.get("student_number") or profile.get("hku_student_id") or ""),
        "faculty": str(academic.get("faculty") or profile.get("faculty") or ""),
        "programme": expand_programme_name(academic.get("programme") or profile.get("programme") or ""),
        "programme_full": expand_programme_name(academic.get("programme") or profile.get("programme") or ""),
        "gpa": str(academic.get("gpa") or ""),
        "year_of_study": year_label or str(year or ""),
        "current_year_of_study": year_label or str(year or ""),
        "expected_graduation_year": _format_graduation_mm_yy(academic.get("expected_graduation_year")),
        "graduation_year": _format_graduation_mm_yy(academic.get("expected_graduation_year")),
        "date_of_birth": _format_dob_dd_mm_yy(profile.get("date_of_birth") or profile.get("dob") or ""),
        "place_of_birth": str(profile.get("place_of_birth") or nationality.get("country_of_origin") or ""),
        "permanent_resident": _yes_no_permanent_resident(nationality.get("local_status") or profile.get("local_status") or ""),
        "local_status": _yes_no_permanent_resident(nationality.get("local_status") or profile.get("local_status") or ""),
        "home_address": str(profile.get("home_address") or profile.get("address") or contact.get("address") or ""),
        "address": str(profile.get("home_address") or profile.get("address") or contact.get("address") or ""),
        "email": str(profile.get("email") or ""),
        "university_email": _university_email(profile),
        "personal_email": _personal_email(profile),
        "phone": str(profile.get("phone") or contact.get("mobile") or contact.get("phone") or ""),
        "mobile": str(profile.get("phone") or contact.get("mobile") or ""),
        "home_phone": str(profile.get("home_phone") or contact.get("home_phone") or ""),
        "country_of_origin": str(nationality.get("country_of_origin") or ""),
    }


ANCHOR_RESOLVERS: list[tuple[tuple[str, ...], str]] = [
    (("name in english", "english on hkid"), "name_en"),
    (("name in chinese", "chinese on hkid"), "chinese_name"),
    (("preferred name",), "preferred_name"),
    (("gender",), "gender"),
    (("university",), "university"),
    (("student number", "student no"), "student_number"),
    (("year gpa", "gpa"), "gpa"),
    (("programme in full", "programme"), "programme_full"),
    (("expected graduation", "graduation year"), "graduation_year"),
    (("current year of study", "year of study"), "current_year_of_study"),
    (("date of birth", "dob"), "date_of_birth"),
    (("place of birth",), "place_of_birth"),
    (("permanent resident", "hksar"), "permanent_resident"),
    (("home address",), "home_address"),
    (("personal email",), "personal_email"),
    (("university email",), "university_email"),
    (("contact number (mobile)", "mobile", "contact number mobile"), "mobile"),
    (("contact number (home)", "home phone", "contact number home"), "home_phone"),
    (("faculty",), "faculty"),
    (("email address", "email"), "email"),
]


def resolve_by_anchor(anchor_label: str, profile: dict) -> str:
    anchor = normalize_text(anchor_label)
    if not anchor:
        return ""
    flat = _profile_flat(profile)
    for patterns, key in ANCHOR_RESOLVERS:
        if any(pattern in anchor for pattern in patterns):
            value = flat.get(key, "")
            if value:
                return value
    return ""


def resolve_profile_field(profile: dict, field_schema: dict) -> str:
    """Resolve a simple form field value from profile data."""
    profile_key = str(field_schema.get("profile_key") or field_schema.get("key") or "").strip()
    flat = _profile_flat(profile)

    if profile_key:
        if profile_key in flat and flat[profile_key]:
            return flat[profile_key]
        legacy = {
            "name": flat["name"],
            "email": flat["email"],
            "phone": flat["phone"],
            "student_id": flat["student_id"],
            "faculty": flat["faculty"],
            "programme": flat["programme"],
            "gpa": flat["gpa"],
            "year_of_study": flat["year_of_study"],
            "country_of_origin": flat["country_of_origin"],
            "local_status": flat["local_status"],
        }
        if profile_key in legacy and legacy[profile_key]:
            return legacy[profile_key]

    anchor_value = resolve_by_anchor(field_schema.get("anchor_label", ""), profile)
    if anchor_value:
        return anchor_value

    key = str(field_schema.get("key") or "")
    if key in flat and flat[key]:
        return flat[key]

    return resolve_by_anchor(key.replace("_", " "), profile)
