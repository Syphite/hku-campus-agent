"""Unit tests for scholarship matching helpers."""

import unittest

from agent.matching import (
    _deterministic_program_match,
    _faculty_matches,
    _student_programme_groups,
    _year_matches,
)

CDS_PROFILE = {
    "academic": {
        "faculty": "School of Computing and Data Science",
        "programme": "Bachelor of Science in Computer Science",
        "year_of_study": "Year 1",
        "level": "undergraduate",
        "gpa": 3.8,
        "nationality": {"local_status": "local"},
    },
    "financial": {"financial_need_opt_in": False},
}

ENGINEERING_PROFILE = {
    "academic": {
        "faculty": "Faculty of Engineering",
        "programme": "Bachelor of Engineering in Civil Engineering",
        "year_of_study": "Year 2",
        "level": "undergraduate",
        "gpa": 3.5,
        "nationality": {"local_status": "local"},
    },
    "financial": {"financial_need_opt_in": False},
}


class FacultyMatchingTests(unittest.TestCase):
    def test_cds_does_not_match_engineering_faculty_scholarship(self):
        self.assertFalse(
            _faculty_matches(
                ["Faculty of Engineering"],
                "School of Computing and Data Science",
            )
        )

    def test_cds_matches_cds_label(self):
        self.assertTrue(
            _faculty_matches(["CDS"], "School of Computing and Data Science")
        )

    def test_cds_matches_full_school_name(self):
        self.assertTrue(
            _faculty_matches(
                ["School of Computing and Data Science"],
                "School of Computing and Data Science",
            )
        )

    def test_university_wide_matches_any_student(self):
        self.assertTrue(
            _faculty_matches(["all"], "School of Computing and Data Science")
        )

    def test_engineering_student_matches_engineering_scholarship(self):
        self.assertTrue(
            _faculty_matches(["Faculty of Engineering"], "Faculty of Engineering")
        )


class YearMatchingTests(unittest.TestCase):
    def test_year_one_string_matches_index_year(self):
        self.assertTrue(_year_matches(["1", "2", "3"], "Year 1"))

    def test_penultimate_matches_year_three(self):
        self.assertTrue(_year_matches(["penultimate"], "Year 3"))


class ProgrammeMatchTests(unittest.TestCase):
    def test_engineering_scholarship_is_mismatch_for_cds_student(self):
        item = {
            "name": "Faculty of Engineering Innovation Scholarship",
            "faculty": ["Faculty of Engineering"],
            "eligibility_raw": "Open to undergraduate students in the Faculty of Engineering.",
        }
        self.assertEqual(_deterministic_program_match(item, CDS_PROFILE), "mismatch")

    def test_university_wide_award_is_faculty_only(self):
        item = {
            "name": "HKU Merit Scholarship",
            "faculty": ["all"],
            "eligibility_raw": "Open to all HKU undergraduate students with strong academic performance.",
        }
        self.assertEqual(_deterministic_program_match(item, CDS_PROFILE), "faculty_only")

    def test_cds_faculty_award_matches_cds_student(self):
        item = {
            "name": "CDS Excellence Scholarship",
            "faculty": ["School of Computing and Data Science"],
            "eligibility_raw": "Open to undergraduate students in the School of Computing and Data Science.",
        }
        self.assertEqual(_deterministic_program_match(item, CDS_PROFILE), "faculty_only")

    def test_medicine_only_award_is_mismatch(self):
        item = {
            "name": "Medicine Clinical Scholarship",
            "faculty": ["Faculty of Medicine"],
            "eligibility_raw": "Only for students enrolled in the MBBS programme.",
        }
        self.assertEqual(_deterministic_program_match(item, CDS_PROFILE), "mismatch")

    def test_cds_student_programme_groups_exclude_engineering(self):
        groups = _student_programme_groups(
            CDS_PROFILE["academic"]["programme"],
            CDS_PROFILE["academic"]["faculty"],
        )
        self.assertIn("computer science", groups)
        self.assertNotIn("engineering", groups)

    def test_engineering_student_programme_groups_include_engineering(self):
        groups = _student_programme_groups(
            ENGINEERING_PROFILE["academic"]["programme"],
            ENGINEERING_PROFILE["academic"]["faculty"],
        )
        self.assertIn("engineering", groups)


if __name__ == "__main__":
    unittest.main()
