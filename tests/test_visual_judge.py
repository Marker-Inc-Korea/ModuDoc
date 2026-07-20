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

    def test_visible_attachment_notice_is_reviewed_instead_of_hard_failed(self):
        data = {
            "low_confidence": True,
            "elements": [
                {
                    "type": "text",
                    "content": "Protected attachment notice with the source filename.",
                    "_source": "eml_notice",
                    "_confidence": 0.35,
                }
            ],
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

    def test_page_counter_suffix_is_removed_before_text_comparison(self):
        verdict = {
            "pass": False,
            "score": 35,
            "severity": "major",
            "issue_types": ["wrong_text", "wrong_order", "hallucination"],
            "text_mismatches": [
                {
                    "image_text": "Appendix transaction report",
                    "candidate_text": "Appendix transaction report - 18 -",
                }
            ],
            "hallucinated_candidate_text": [
                "Appendix transaction report - 18 -"
            ],
            "structure_evidence": [
                "The extra '- 18 -' appears in the header instead of the footer."
            ],
        }

        result = visual_judge_pages.stabilize_verdict(
            verdict, "Appendix transaction report - 18 -"
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["issue_types"], [])
        self.assertEqual(result["text_mismatches"], [])
        self.assertEqual(result["hallucinated_candidate_text"], [])
        self.assertEqual(result["structure_evidence"], [])

    def test_page_counter_structure_claim_is_ignored_without_position_wording(self):
        verdict = {
            "pass": False,
            "score": 45,
            "severity": "major",
            "issue_types": ["wrong_order"],
            "structure_evidence": [
                "The title is combined with the printed counter '- 1 8 -'."
            ],
        }

        result = visual_judge_pages.stabilize_verdict(
            verdict, "Appendix transaction report - 1 8 -"
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["structure_evidence"], [])

    def test_element_type_only_claim_is_not_wrong_order(self):
        verdict = {
            "pass": False,
            "score": 65,
            "severity": "major",
            "issue_types": ["wrong_order"],
            "structure_evidence": [
                "The image has a heading, but the candidate stores it as a text element."
            ],
        }

        result = visual_judge_pages.stabilize_verdict(
            verdict, "Section heading Body content"
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["structure_evidence"], [])

    def test_caption_metadata_has_no_above_or_below_position(self):
        primary = {
            "pass": False,
            "score": 70,
            "severity": "major",
            "issue_types": ["table_structure"],
        }
        review = {
            "confirmed_failure": True,
            "confirmed_issue_types": ["table_structure"],
            "structure_evidence": [
                "The candidate caption is positioned below the table instead of above it."
            ],
        }

        result = visual_judge_pages.apply_failure_review(primary, review)

        self.assertTrue(result["pass"])

    def test_wrong_card_association_remains_structural_evidence(self):
        primary = {
            "pass": False,
            "score": 55,
            "severity": "major",
            "issue_types": ["wrong_order"],
        }
        review = {
            "confirmed_failure": True,
            "confirmed_issue_types": ["wrong_order"],
            "structure_evidence": [
                "The second card's bullets are grouped under the third card title."
            ],
        }

        result = visual_judge_pages.apply_failure_review(primary, review)

        self.assertFalse(result["pass"])
        self.assertEqual(result["issue_types"], ["wrong_order"])

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

    def test_review_ignores_page_counter_attached_to_a_heading(self):
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
                {
                    "image_text": "Appendix transaction report",
                    "candidate_text": "Appendix transaction report - 18 -",
                }
            ],
        }

        result = visual_judge_pages.apply_failure_review(
            primary, review, "Appendix transaction report - 18 -"
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

    def test_review_rejects_cosmetic_table_evidence(self):
        primary = {
            "pass": False,
            "score": 85,
            "severity": "major",
            "issue_types": ["table_structure"],
        }
        review = {
            "confirmed_failure": True,
            "confirmed_issue_types": ["table_structure"],
            "structure_evidence": [
                "The header has an extra space and a minor alignment difference."
            ],
            "reason": "This is only a minor formatting error; no content is wrong.",
        }

        result = visual_judge_pages.apply_failure_review(primary, review)

        self.assertTrue(result["pass"])
        self.assertEqual(result["issue_types"], [])

    def test_review_keeps_material_table_evidence(self):
        primary = {
            "pass": False,
            "score": 60,
            "severity": "major",
            "issue_types": ["table_structure"],
        }
        review = {
            "confirmed_failure": True,
            "confirmed_issue_types": ["table_structure"],
            "structure_evidence": [
                "The image has four columns, but the candidate has a missing column."
            ],
            "reason": "A data column is absent.",
        }

        result = visual_judge_pages.apply_failure_review(primary, review)

        self.assertFalse(result["pass"])
        self.assertEqual(result["issue_types"], ["table_structure"])

    def test_review_keeps_material_alignment_evidence(self):
        primary = {
            "pass": False,
            "score": 55,
            "severity": "major",
            "issue_types": ["table_structure"],
        }
        review = {
            "confirmed_failure": True,
            "confirmed_issue_types": ["table_structure"],
            "structure_evidence": [
                "Cell alignment shifted a value under the wrong header."
            ],
            "reason": "The alignment error changes the value-to-header association.",
        }

        result = visual_judge_pages.apply_failure_review(primary, review)

        self.assertFalse(result["pass"])
        self.assertEqual(result["issue_types"], ["table_structure"])


if __name__ == "__main__":
    unittest.main()
