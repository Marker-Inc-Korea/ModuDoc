import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from tools import visual_judge_pages


class VisualJudgeTests(unittest.TestCase):
    def test_compact_structured_includes_authoritative_element_indexes(self):
        payload = {
            "page_number": 3,
            "elements": [
                {"type": "heading_2", "content": "First section"},
                {"type": "text", "content": "Body"},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "page.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            compact = json.loads(
                visual_judge_pages.compact_structured(str(path), 10000)
            )

        self.assertEqual(
            [element["index"] for element in compact["elements"]], [0, 1]
        )

    def test_primary_timeout_retries_with_compact_system_and_full_budget(self):
        calls = []

        class Completions:
            @staticmethod
            def create(**kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    raise TimeoutError("simulated timeout")
                verdict = {
                    "pass": True,
                    "score": 100,
                    "severity": "none",
                    "issue_types": [],
                    "missing_visible_text": [],
                    "text_mismatches": [],
                    "hallucinated_candidate_text": [],
                    "structure_evidence": [],
                    "reason": "The visible content matches.",
                }
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=json.dumps(verdict)),
                            finish_reason="stop",
                        )
                    ]
                )

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=Completions())
        )
        args = SimpleNamespace(
            structured_limit=10000,
            max_width=1400,
            retries=1,
            max_tokens=16384,
            timeout=1,
            review_failures=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_path = root / "page_0001.png"
            structured_path = root / "page_0001_structured.json"
            Image.new("RGB", (200, 200), "white").save(image_path)
            structured_path.write_text(
                json.dumps(
                    {
                        "page_number": 1,
                        "elements": [{"type": "text", "content": "Visible"}],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(visual_judge_pages.time, "sleep"):
                result = visual_judge_pages.judge_one(
                    client,
                    "test-model",
                    {
                        "doc": "test",
                        "page": 1,
                        "image": str(image_path),
                        "structured": str(structured_path),
                    },
                    args,
                )

        self.assertTrue(result["pass"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0]["messages"][0]["content"], visual_judge_pages.SYSTEM
        )
        self.assertEqual(
            calls[1]["messages"][0]["content"], visual_judge_pages.RETRY_SYSTEM
        )
        self.assertEqual([call["max_tokens"] for call in calls], [16384, 16384])

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

    def test_structure_facts_tolerate_invalid_table_spans(self):
        data = {
            "elements": [
                {
                    "type": "table",
                    "content": (
                        '<table><tr><td colspan="invalid">A</td></tr></table>'
                    ),
                }
            ]
        }

        facts = visual_judge_pages.candidate_structure_facts(data)

        self.assertEqual(facts["table_elements"][0]["rows"], 1)
        self.assertEqual(facts["table_elements"][0]["columns"], 1)
        self.assertEqual(facts["element_types"], {0: "table"})

    def test_structure_facts_identify_leading_rows_already_in_caption(self):
        data = {
            "elements": [
                {
                    "type": "table",
                    "caption": "Funding plan (USD millions)",
                    "content": (
                        "<table><tr><th colspan='2'>Funding plan</th></tr>"
                        "<tr><th>(USD millions)</th><th></th></tr>"
                        "<tr><td>Use</td><td>Amount</td></tr></table>"
                    ),
                }
            ]
        }

        facts = visual_judge_pages.candidate_structure_facts(data)

        self.assertEqual(facts["table_elements"][0]["caption_prefix_rows"], 2)

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
        self.assertTrue(visual_judge_pages.is_notice_only_page(data))

    def test_html_entities_do_not_create_text_mismatches(self):
        verdict = {
            "pass": False,
            "score": 60,
            "severity": "major",
            "issue_types": ["wrong_text"],
            "text_mismatches": [
                {
                    "image_text": "Standards & Practice",
                    "candidate_text": "Standards &amp; Practice",
                }
            ],
        }

        result = visual_judge_pages.stabilize_verdict(
            verdict, "Standards &amp; Practice"
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["issue_types"], [])

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

    def test_unindexed_structural_claim_is_rejected_when_candidate_facts_exist(self):
        verdict = {
            "pass": False,
            "score": 60,
            "severity": "major",
            "issue_types": ["wrong_order", "table_structure"],
            "structure_evidence": [
                "The candidate order differs from the image.",
                "The image has three rows while the candidate has two rows.",
            ],
        }
        facts = {
            "element_indices": [0, 1],
            "table_elements": {1: {"rows": 3, "columns": 2, "table_tags": 1}},
        }

        result = visual_judge_pages.stabilize_verdict(
            verdict, "First Second", facts
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["issue_types"], [])

    def test_indexed_geometry_claim_remains_a_structural_failure(self):
        verdict = {
            "pass": False,
            "score": 60,
            "severity": "major",
            "issue_types": ["table_structure"],
            "structure_evidence": [
                "The image has 4 rows, while candidate element index 1 has 3 rows."
            ],
        }
        facts = {
            "element_indices": [0, 1],
            "table_elements": {1: {"rows": 3, "columns": 2, "table_tags": 1}},
        }

        result = visual_judge_pages.stabilize_verdict(
            verdict, "First Second", facts
        )

        self.assertFalse(result["pass"])
        self.assertEqual(result["issue_types"], ["table_structure"])

    def test_candidate_geometry_claim_must_match_authoritative_html_facts(self):
        verdict = {
            "pass": False,
            "score": 60,
            "severity": "major",
            "issue_types": ["table_structure"],
            "structure_evidence": [
                "The image has 3 rows, while candidate element index 1 has 2 rows."
            ],
        }
        facts = {
            "element_indices": [0, 1],
            "table_elements": {
                1: {"rows": 3, "header_rows": 1, "columns": 3, "table_tags": 1}
            },
        }

        result = visual_judge_pages.stabilize_verdict(
            verdict, "Header First row Second row", facts
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["issue_types"], [])

    def test_table_row_claim_cannot_silently_exclude_the_header(self):
        verdict = {
            "pass": False,
            "score": 60,
            "severity": "major",
            "issue_types": ["table_structure"],
            "structure_evidence": [
                "Candidate element index 1 has 3 rows, but the image has 2 rows."
            ],
        }
        facts = {
            "element_indices": [0, 1],
            "table_elements": {
                1: {"rows": 3, "header_rows": 1, "columns": 3, "table_tags": 1}
            },
        }

        result = visual_judge_pages.stabilize_verdict(
            verdict, "Header First row Second row", facts
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["issue_types"], [])

    def test_table_row_claim_cannot_silently_exclude_caption_rows(self):
        verdict = {
            "pass": False,
            "score": 60,
            "severity": "major",
            "issue_types": ["table_structure"],
            "structure_evidence": [
                "Candidate element index 1 has 14 rows, but the image has 13 rows."
            ],
        }
        facts = {
            "element_indices": [0, 1],
            "table_elements": {
                1: {
                    "rows": 14,
                    "header_rows": 2,
                    "columns": 4,
                    "table_tags": 1,
                    "caption_prefix_rows": 2,
                }
            },
        }

        result = visual_judge_pages.stabilize_verdict(
            verdict, "Funding plan table", facts
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["issue_types"], [])

    def test_orphan_hallucination_evidence_does_not_create_an_undeclared_issue(self):
        verdict = {
            "pass": False,
            "score": 60,
            "severity": "major",
            "issue_types": ["table_structure"],
            "hallucinated_candidate_text": ["Visible footnote"],
            "structure_evidence": [],
        }

        result = visual_judge_pages.stabilize_verdict(
            verdict, "Visible footnote"
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["hallucinated_candidate_text"], [])

    def test_indexed_order_claim_requires_two_candidate_element_references(self):
        verdict = {
            "pass": False,
            "score": 60,
            "severity": "major",
            "issue_types": ["wrong_order"],
            "structure_evidence": [
                "Candidate element index 1 precedes candidate element index 0, but the image reverses them."
            ],
        }
        facts = {"element_indices": [0, 1], "table_elements": {}}

        result = visual_judge_pages.stabilize_verdict(
            verdict, "First Second", facts
        )

        self.assertFalse(result["pass"])
        self.assertEqual(result["issue_types"], ["wrong_order"])

    def test_indexed_card_body_misassignment_is_a_structural_failure(self):
        verdict = {
            "pass": False,
            "score": 55,
            "severity": "major",
            "issue_types": ["panel_assignment"],
            "structure_evidence": [
                "In the image the Search card owns the lookup bullet, but candidate element index 2 places that body under the wrong Privacy card title."
            ],
        }
        facts = {"element_indices": [0, 1, 2], "table_elements": {}}

        result = visual_judge_pages.stabilize_verdict(
            verdict, "Search Privacy lookup", facts
        )

        self.assertFalse(result["pass"])
        self.assertEqual(result["issue_types"], ["panel_assignment"])

    def test_figure_internal_panels_are_not_panel_assignment_evidence(self):
        verdict = {
            "pass": False,
            "score": 55,
            "severity": "major",
            "issue_types": ["panel_assignment"],
            "structure_evidence": [
                "Candidate element index 2 combines three distinct panels into one figure description."
            ],
        }
        facts = {
            "element_indices": [0, 1, 2],
            "element_types": {0: "heading_1", 1: "text", 2: "figure"},
            "table_elements": {},
        }

        result = visual_judge_pages.stabilize_verdict(
            verdict, "Three visible panel descriptions", facts
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["issue_types"], [])

    def test_combined_paragraph_is_representation_only_without_card_mix(self):
        representation = (
            "Candidate element index 2 combines visible steps into a single paragraph."
        )
        material = (
            "Candidate element index 2 combines text from the Search card under "
            "the wrong Privacy card title."
        )

        self.assertTrue(
            visual_judge_pages.looks_schema_representation_only(representation)
        )
        self.assertFalse(
            visual_judge_pages.looks_schema_representation_only(material)
        )

    def test_mixed_panel_text_is_reclassified_from_generic_wrong_order(self):
        verdict = {
            "pass": False,
            "score": 55,
            "severity": "major",
            "issue_types": ["wrong_order"],
            "structure_evidence": [
                "Candidate element index 2 contains text from multiple distinct feature panels, causing incorrect grouping."
            ],
        }
        facts = {"element_indices": [0, 1, 2], "table_elements": {}}

        result = visual_judge_pages.stabilize_verdict(
            verdict, "Feature one Feature two", facts
        )

        self.assertFalse(result["pass"])
        self.assertEqual(result["issue_types"], ["panel_assignment"])

    def test_ungrounded_review_cannot_overturn_indexed_panel_failure(self):
        primary = {
            "pass": False,
            "score": 55,
            "severity": "major",
            "issue_types": ["panel_assignment"],
            "structure_evidence": [
                "The image shows the Search card body separately, while candidate element index 1 mixes it under a distinct Privacy card title."
            ],
            "reason": "The indexed card body is misassigned.",
        }
        review = {
            "confirmed_failure": False,
            "confirmed_issue_types": [],
            "rejected_claims": ["The sequence appears correct."],
            "reason": "No issue found.",
        }
        facts = {"element_indices": [0, 1], "table_elements": {}}

        result = visual_judge_pages.apply_failure_review(
            primary, review, "First Second", facts
        )

        self.assertFalse(result["pass"])
        self.assertEqual(result["issue_types"], ["panel_assignment"])
        self.assertEqual(result["reason"], primary["reason"])

    def test_false_review_flag_cannot_use_stale_confirmed_types_to_overturn(self):
        primary = {
            "pass": False,
            "score": 55,
            "severity": "major",
            "issue_types": ["panel_assignment"],
            "structure_evidence": [
                "The image shows the Search card body separately, while candidate element index 1 mixes it under a distinct Privacy card title."
            ],
        }
        review = {
            "confirmed_failure": False,
            "confirmed_issue_types": ["panel_assignment"],
            "structure_evidence": [
                "Candidate element index 1 mixes a distinct card body in the image."
            ],
            "rejected_claims": [],
        }
        facts = {"element_indices": [0, 1], "table_elements": {}}

        result = visual_judge_pages.apply_failure_review(
            primary, review, "First Second", facts
        )

        self.assertFalse(result["pass"])
        self.assertEqual(result["issue_types"], ["panel_assignment"])

    def test_grounded_review_can_overturn_indexed_panel_failure(self):
        primary = {
            "pass": False,
            "score": 55,
            "severity": "major",
            "issue_types": ["panel_assignment"],
            "structure_evidence": [
                "The image shows the Search card body separately, while candidate element index 1 mixes it under a distinct Privacy card title."
            ],
        }
        review = {
            "confirmed_failure": False,
            "confirmed_issue_types": [],
            "rejected_claims": [
                "The page image shows the Search body under its Search card title, so candidate element index 1 does not mix distinct card bodies."
            ],
            "reason": "The indexed panel assignment matches the image.",
        }
        facts = {"element_indices": [0, 1], "table_elements": {}}

        result = visual_judge_pages.apply_failure_review(
            primary, review, "First Second", facts
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["issue_types"], [])

    def test_equal_indexed_table_geometry_can_refute_a_primary_failure(self):
        primary = {
            "pass": False,
            "score": 60,
            "severity": "major",
            "issue_types": ["table_structure"],
            "structure_evidence": [
                "The image has 4 rows, but candidate element index 1 has 3 rows."
            ],
        }
        review = {
            "confirmed_failure": False,
            "confirmed_issue_types": [],
            "rejected_claims": [
                "Candidate element index 1 has 3 rows and 2 columns, matching the visual image with 3 rows and 2 columns."
            ],
            "reason": "The indexed geometry matches the image.",
        }
        facts = {
            "element_indices": [0, 1],
            "table_elements": {
                1: {"rows": 3, "header_rows": 1, "columns": 2, "table_tags": 1}
            },
        }

        result = visual_judge_pages.apply_failure_review(
            primary, review, "Header A B", facts
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["issue_types"], [])

    def test_complete_ordinal_table_geometry_can_refute_without_indexes(self):
        primary = {
            "pass": False,
            "score": 60,
            "severity": "major",
            "issue_types": ["table_structure"],
            "structure_evidence": [
                "The image has 13 rows, but candidate element index 2 has 12 rows."
            ],
        }
        review = {
            "confirmed_failure": False,
            "confirmed_issue_types": [],
            "rejected_claims": [
                "The page image has two tables: the first has 2 rows including "
                "the header and 2 columns, and the second has 12 rows including "
                "the header and 2 columns; the candidate matches both tables."
            ],
            "reason": "The ordered table geometry matches the image.",
        }
        facts = {
            "element_indices": [0, 1, 2],
            "element_types": {0: "table", 1: "heading_2", 2: "table"},
            "table_elements": {
                0: {"rows": 2, "columns": 2, "table_tags": 1},
                2: {"rows": 12, "columns": 2, "table_tags": 1},
            },
        }

        result = visual_judge_pages.apply_failure_review(
            primary, review, "First table Second table", facts
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["issue_types"], [])

        review["rejected_claims"][0] = review["rejected_claims"][0].replace(
            "12 rows", "13 rows"
        )
        result = visual_judge_pages.apply_failure_review(
            primary, review, "First table Second table", facts
        )
        self.assertFalse(result["pass"])

    def test_indexed_distinct_tables_can_refute_a_false_merge_claim(self):
        primary = {
            "pass": False,
            "score": 60,
            "severity": "major",
            "issue_types": ["table_structure"],
            "structure_evidence": [
                "The image has one table with 5 rows and 8 columns, but candidate element "
                "index 2 has 5 rows and 8 columns and candidate element index 3 "
                "is a separate table with 2 rows and 10 columns."
            ],
        }
        review = {
            "confirmed_failure": False,
            "confirmed_issue_types": [],
            "rejected_claims": [
                "The page image contains two distinct tables. The first table "
                "(candidate element index 2) has 5 rows and 8 columns, and the "
                "second table (candidate element index 3) has 2 rows and 10 "
                "columns. The candidate correctly represents both tables."
            ],
            "reason": "The indexed table geometry matches the image.",
        }
        facts = {
            "element_indices": list(range(8)),
            "element_types": {
                **{index: "text" for index in range(8)},
                2: "table",
                3: "table",
                6: "table",
            },
            "table_elements": {
                2: {"rows": 5, "columns": 8, "table_tags": 1},
                3: {"rows": 2, "columns": 10, "table_tags": 1},
                6: {"rows": 4, "columns": 3, "table_tags": 1},
            },
        }

        result = visual_judge_pages.apply_failure_review(
            primary, review, "First table Second table Other table", facts
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["issue_types"], [])

        review["rejected_claims"][0] = review["rejected_claims"][0].replace(
            "2 rows and 10 columns", "2 rows and 9 columns"
        )
        result = visual_judge_pages.apply_failure_review(
            primary, review, "First table Second table Other table", facts
        )
        self.assertFalse(result["pass"])

    def test_complete_single_table_geometry_can_refute_without_an_index(self):
        primary = {
            "pass": False,
            "score": 60,
            "severity": "major",
            "issue_types": ["table_structure"],
            "structure_evidence": [
                "The image has 10 rows, but candidate element index 3 has 11 rows."
            ],
        }
        review = {
            "confirmed_failure": False,
            "confirmed_issue_types": [],
            "rejected_claims": [
                "The candidate table has 11 rows including the header and 11 columns, "
                "matching the page image."
            ],
            "reason": "The single table geometry matches the image.",
        }
        facts = {
            "element_indices": [0, 1, 2, 3],
            "element_types": {0: "heading_1", 1: "text", 2: "text", 3: "table"},
            "table_elements": {
                3: {"rows": 11, "header_rows": 2, "columns": 11, "table_tags": 1}
            },
        }

        result = visual_judge_pages.apply_failure_review(
            primary, review, "Header and rows", facts
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["issue_types"], [])

        review["rejected_claims"][0] = review["rejected_claims"][0].replace(
            "11 rows", "10 rows"
        )
        result = visual_judge_pages.apply_failure_review(
            primary, review, "Header and rows", facts
        )
        self.assertFalse(result["pass"])

    def test_single_table_exact_geometry_can_be_implicit_in_review(self):
        primary = {
            "pass": False,
            "score": 60,
            "severity": "major",
            "issue_types": ["table_structure"],
            "structure_evidence": [
                "The image has 30 rows, but candidate element index 6 has 28 rows."
            ],
        }
        review = {
            "confirmed_failure": False,
            "confirmed_issue_types": [],
            "rejected_claims": [
                "The table has 28 rows and 7 columns, which matches the image. "
                "There is one table element (index 6)."
            ],
            "reason": "No material table structure error was found.",
        }
        facts = {
            "element_indices": list(range(7)),
            "element_types": {**{index: "text" for index in range(6)}, 6: "table"},
            "table_elements": {
                6: {"rows": 28, "header_rows": 2, "columns": 7, "table_tags": 1}
            },
        }

        result = visual_judge_pages.apply_failure_review(
            primary, review, "Table and preceding notes", facts
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["issue_types"], [])

        review["rejected_claims"][0] = review["rejected_claims"][0].replace(
            "28 rows", "27 rows"
        )
        result = visual_judge_pages.apply_failure_review(
            primary, review, "Table and preceding notes", facts
        )
        self.assertFalse(result["pass"])

    def test_matching_review_structure_evidence_can_act_as_refutation(self):
        primary = {
            "pass": False,
            "score": 60,
            "severity": "major",
            "issue_types": ["table_structure"],
            "structure_evidence": [
                "The image has 3 columns, but candidate element index 0 has 5 columns."
            ],
        }
        review = {
            "confirmed_failure": False,
            "confirmed_issue_types": [],
            "structure_evidence": [
                "Candidate element index 0 has 9 rows and 5 columns, matching "
                "the visual image with 9 rows and 5 columns."
            ],
            "rejected_claims": [],
            "reason": "The indexed table geometry matches.",
        }
        facts = {
            "element_indices": [0],
            "element_types": {0: "table"},
            "table_elements": {
                0: {"rows": 9, "header_rows": 1, "columns": 5, "table_tags": 1}
            },
        }

        result = visual_judge_pages.apply_failure_review(
            primary, review, "Header and rows", facts
        )

        self.assertTrue(result["pass"])
        self.assertEqual(result["issue_types"], [])

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
