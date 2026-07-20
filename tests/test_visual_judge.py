import unittest

from tools import visual_judge_pages


class VisualJudgeTests(unittest.TestCase):
    def test_objective_quality_metadata_cannot_be_overturned(self):
        data = {
            "elements": [
                {
                    "type": "table",
                    "content": "<table><tr><td>A</td></tr></table>",
                    "_confidence": 0.65,
                    "_issues": ["ragged_rows", "nested_table_kept"],
                }
            ]
        }

        failures = visual_judge_pages.objective_quality_failures(data)

        self.assertIn("element_0_low_confidence", failures)
        self.assertIn("element_0_ragged_rows", failures)
        self.assertFalse(any("nested_table_kept" in item for item in failures))

    def test_normal_quality_metadata_has_no_objective_failure(self):
        data = {
            "elements": [
                {
                    "type": "table",
                    "content": "<table><tr><td>A</td></tr></table>",
                    "_confidence": 0.75,
                    "_issues": ["nested_table_kept"],
                }
            ]
        }

        self.assertEqual(visual_judge_pages.objective_quality_failures(data), [])

    def test_page_counter_is_not_treated_as_missing_content(self):
        verdict = {
            "pass": False,
            "score": 72,
            "severity": "major",
            "issue_types": ["missing_text"],
            "missing_visible_text": ["- / 51 -"],
        }

        result = visual_judge_pages.stabilize_verdict(verdict, "Body text")

        self.assertTrue(result["pass"])
        self.assertEqual(result["missing_visible_text"], [])
        self.assertEqual(result["issue_types"], [])

    def test_plain_numeric_content_is_not_assumed_to_be_a_page_counter(self):
        self.assertFalse(visual_judge_pages.looks_like_page_artifact("2024"))
        self.assertTrue(visual_judge_pages.looks_like_page_artifact("Page 12 of 30"))

    def test_printed_pagination_is_not_structural_failure_evidence(self):
        verdict = {
            "pass": False,
            "score": 50,
            "severity": "major",
            "issue_types": ["wrong_order"],
            "structure_evidence": [
                "The candidate page number is 70 while printed pagination is 51."
            ],
        }

        result = visual_judge_pages.stabilize_verdict(verdict, "Body text")

        self.assertTrue(result["pass"])
        self.assertEqual(result["issue_types"], [])
        self.assertEqual(result["structure_evidence"], [])

    def test_review_can_reject_an_unsupported_failure(self):
        primary = {
            "pass": False,
            "score": 62,
            "severity": "major",
            "issue_types": ["wrong_order"],
            "structure_evidence": ["Primary order claim"],
            "reason": "Order differs",
        }
        review = {
            "confirmed_failure": False,
            "confirmed_issue_types": [],
            "evidence": [],
            "reason": "The image and candidate use the same order.",
        }

        result = visual_judge_pages.apply_failure_review(primary, review)

        self.assertTrue(result["pass"])
        self.assertEqual(result["issue_types"], [])
        self.assertEqual(result["severity"], "none")

    def test_review_keeps_only_confirmed_primary_issue_types(self):
        primary = {
            "pass": False,
            "score": 40,
            "severity": "critical",
            "issue_types": ["missing_text", "table_structure"],
            "reason": "Multiple issues",
        }
        review = {
            "confirmed_failure": True,
            "confirmed_issue_types": ["table_structure", "hallucination"],
            "structure_evidence": ["The image has three columns and the candidate has two."],
            "reason": "The visible grid has one more column.",
        }

        result = visual_judge_pages.apply_failure_review(primary, review)

        self.assertFalse(result["pass"])
        self.assertEqual(result["issue_types"], ["table_structure"])
        self.assertEqual(result["reason"], review["reason"])

    def test_review_cannot_replace_primary_failure_with_a_new_issue(self):
        primary = {
            "pass": False,
            "score": 70,
            "severity": "major",
            "issue_types": ["table_structure"],
            "reason": "Primary claim",
        }
        review = {
            "confirmed_failure": True,
            "confirmed_issue_types": ["wrong_text"],
            "evidence": ["Only a spacing difference is visible."],
            "reason": "The primary table claim should be rejected.",
        }

        result = visual_judge_pages.apply_failure_review(primary, review)

        self.assertTrue(result["pass"])
        self.assertEqual(result["issue_types"], [])

    def test_review_wrong_text_requires_a_verifiable_pair(self):
        primary = {
            "pass": False,
            "score": 60,
            "severity": "major",
            "issue_types": ["wrong_text"],
            "reason": "Text differs",
        }
        unsupported = {
            "confirmed_failure": True,
            "confirmed_issue_types": ["wrong_text"],
            "text_mismatches": [],
            "reason": "A difference exists.",
        }
        supported = {
            "confirmed_failure": True,
            "confirmed_issue_types": ["wrong_text"],
            "text_mismatches": [
                {"image_text": "Correct term", "candidate_text": "Incorrect term"}
            ],
            "reason": "The visible terms differ.",
        }

        rejected = visual_judge_pages.apply_failure_review(
            primary, unsupported, "Incorrect term"
        )
        accepted = visual_judge_pages.apply_failure_review(
            primary, supported, "Incorrect term"
        )

        self.assertTrue(rejected["pass"])
        self.assertFalse(accepted["pass"])

    def test_review_ignores_page_counter_text_mismatches(self):
        primary = {
            "pass": False,
            "score": 60,
            "severity": "major",
            "issue_types": ["wrong_text"],
        }
        review = {
            "confirmed_failure": True,
            "confirmed_issue_types": ["wrong_text"],
            "text_mismatches": [
                {"image_text": "Page 12", "candidate_text": "Page 13"}
            ],
        }

        result = visual_judge_pages.apply_failure_review(
            primary, review, "Page 13 Body"
        )

        self.assertTrue(result["pass"])

    def test_review_structure_requires_structure_evidence(self):
        primary = {
            "pass": False,
            "score": 60,
            "severity": "major",
            "issue_types": ["wrong_order"],
        }
        review = {
            "confirmed_failure": True,
            "confirmed_issue_types": ["wrong_order"],
            "evidence": ["A generic concern without sequence comparison."],
        }

        result = visual_judge_pages.apply_failure_review(primary, review)

        self.assertTrue(result["pass"])


if __name__ == "__main__":
    unittest.main()
