from pathlib import Path
import tempfile
import unittest

from bs4 import BeautifulSoup
from PIL import Image, ImageDraw

import table_validate
import table_quality_repair


def _visible_text(html):
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


class GenericTableRepairTests(unittest.TestCase):
    def test_table_repair_image_trims_only_large_blank_margins(self):
        image = Image.new("RGB", (1000, 1400), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((100, 800, 900, 1200), outline="black", width=4)

        cropped = table_quality_repair._crop_content_bbox(image)

        self.assertLess(cropped.height, image.height * 0.5)
        self.assertGreater(cropped.width, 800)

    def test_table_geometry_uses_the_modal_body_width(self):
        element = {
            "type": "table",
            "content": (
                "<table>"
                "<tr><td>A</td><td>B</td></tr>"
                "<tr><td>1</td><td>2</td><td>3</td></tr>"
                "<tr><td>4</td><td>5</td><td>6</td></tr>"
                "<tr><td>7</td><td>8</td><td>9</td></tr>"
                "</table>"
            ),
        }

        geometry = table_quality_repair._table_geometry(element)

        self.assertEqual(geometry["expanded_row_widths"], [2, 3, 3, 3])
        self.assertEqual(geometry["most_frequent_width"], 3)
        self.assertEqual(geometry["maximum_width"], 3)

    def test_table_text_reference_does_not_expose_broken_spans(self):
        element = {
            "type": "table",
            "caption": "Example",
            "content": (
                "<table><tr><td rowspan='3'>Group</td><td>Value</td></tr>"
                "<tr><td>Second</td></tr></table>"
            ),
        }

        reference = table_quality_repair._table_text_reference(element)

        self.assertEqual(reference["caption"], "Example")
        self.assertEqual(reference["visible_rows"], [["Group", "Value"], ["Second"]])
        self.assertNotIn("rowspan", str(reference))

    def test_geometry_correction_changes_only_spans_and_preserves_text(self):
        candidate = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "content": (
                        "<table><tr><td colspan='3'>Summary</td><td>Tail</td></tr>"
                        "<tr><td>A</td><td>B</td><td>C</td></tr>"
                        "<tr><td>1</td><td>2</td><td>3</td></tr></table>"
                    ),
                }
            ],
        }
        correction = {
            "tables": [
                {
                    "table_index": 0,
                    "choice_ids": ["r0c0_colspan2"],
                }
            ]
        }

        corrected = table_quality_repair._apply_geometry_correction(
            candidate, correction
        )

        self.assertIsNotNone(corrected)
        self.assertEqual(
            _visible_text(corrected["elements"][0]["content"]),
            _visible_text(candidate["elements"][0]["content"]),
        )
        self.assertEqual(
            table_quality_repair._table_geometry(corrected["elements"][0])[
                "expanded_row_widths"
            ],
            [3, 3, 3],
        )
    def test_geometry_correction_can_expose_a_trailing_blank_header_cell(self):
        candidate = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "content": (
                        "<table><tr><td></td><td colspan='4'>Group</td></tr>"
                        "<tr><td>A</td><td>B</td><td>C</td><td>D</td></tr>"
                        "<tr><td>1</td><td>2</td><td>3</td><td>4</td></tr></table>"
                    ),
                }
            ],
        }
        correction = {
            "tables": [
                {
                    "table_index": 0,
                    "choice_ids": ["r0c1_colspan3_split1a"],
                }
            ]
        }

        corrected = table_quality_repair._apply_geometry_correction(
            candidate, correction
        )

        self.assertIsNotNone(corrected)
        table = BeautifulSoup(
            corrected["elements"][0]["content"], "html.parser"
        ).find("table")
        first_row = table.find("tr").find_all(["td", "th"], recursive=False)
        self.assertEqual(len(first_row), 3)
        self.assertEqual([cell.get_text(strip=True) for cell in first_row], ["", "Group", ""])
        self.assertEqual(
            table_quality_repair._table_geometry(corrected["elements"][0])[
                "expanded_row_widths"
            ],
            [4, 4, 4],
        )

        image = Image.new("RGB", (600, 400), "white")
        draw = ImageDraw.Draw(image)
        for y in (50, 150, 250, 350):
            draw.line((50, y, 550, y), fill="black", width=3)
        for upper, lower in ((150, 250), (250, 350)):
            for x in (50, 170, 290, 410, 550):
                draw.line((x, upper, x, lower), fill="black", width=3)
        for x in (50, 170, 410, 550):
            draw.line((x, 50, x, 150), fill="black", width=3)
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "table.png"
            image.save(image_path)
            detected = table_quality_repair._grid_line_geometry_correction(
                image_path, candidate
            )
        self.assertEqual(detected, correction)

    def test_geometry_correction_rejects_a_still_ragged_patch(self):
        candidate = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "content": (
                        "<table><tr><td colspan='3'>Summary</td><td>Tail</td></tr>"
                        "<tr><td>A</td><td>B</td><td>C</td></tr>"
                        "<tr><td>1</td><td>2</td><td>3</td></tr></table>"
                    ),
                }
            ],
        }
        correction = {
            "tables": [
                {
                    "table_index": 0,
                    "choice_ids": ["r0c0_colspan1"],
                }
            ]
        }

        corrected = table_quality_repair._apply_geometry_correction(
            candidate, correction
        )

        self.assertIsNone(corrected)
        self.assertIn("colspan='3'", candidate["elements"][0]["content"])

    def test_blank_column_does_not_split_a_continuous_score_matrix(self):
        html = (
            "<table>"
            "<tr><td>First impact</td><td>L</td><td></td><td>-1</td><td>+2</td></tr>"
            "<tr><td>Second impact</td><td>R</td><td></td><td>0</td><td>+3</td></tr>"
            "<tr><td>Third impact</td><td>I</td><td></td><td>-2</td><td>+1</td></tr>"
            "</table>"
        )

        repaired, needs_retry, issues = table_validate.validate_and_repair_table(html)

        self.assertEqual(len(repaired), 1)
        self.assertEqual(repaired[0]["content"], html)
        self.assertFalse(needs_retry)
        self.assertNotIn(("gutter", 2, 0), issues)

    def test_labeled_grids_separated_by_a_gutter_are_split(self):
        html = (
            "<table>"
            "<tr><td>Item</td><td>Value</td><td></td><td>Region</td><td>Owner</td></tr>"
            "<tr><td>Alpha</td><td>10</td><td></td><td>North</td><td>Kim</td></tr>"
            "<tr><td>Beta</td><td>20</td><td></td><td>South</td><td>Lee</td></tr>"
            "</table>"
        )

        repaired, needs_retry, issues = table_validate.validate_and_repair_table(html)

        self.assertEqual(len(repaired), 2)
        self.assertFalse(needs_retry)
        self.assertIn(("gutter", 2, 0), issues)

    def test_gutter_split_label_detection_supports_unicode_scripts(self):
        html = (
            "<table>"
            "<tr><td>項目</td><td>値</td><td></td><td>地域</td><td>担当</td></tr>"
            "<tr><td>製品甲</td><td>10</td><td></td><td>北部</td><td>佐藤</td></tr>"
            "<tr><td>製品乙</td><td>20</td><td></td><td>南部</td><td>鈴木</td></tr>"
            "</table>"
        )

        repaired, needs_retry, issues = table_validate.validate_and_repair_table(html)

        self.assertEqual(len(repaired), 2)
        self.assertFalse(needs_retry)
        self.assertIn(("gutter", 2, 0), issues)

    def test_rectangular_nested_table_keeps_its_parent_cell(self):
        html = (
            "<table>"
            "<tr><td>Section</td><td><table><tr><td>A</td><td>B</td></tr>"
            "<tr><td>1</td><td>2</td></tr></table></td></tr>"
            "<tr><td>Footer</td><td>Value</td></tr>"
            "</table>"
        )
        elements, retry, issues = table_validate.validate_and_repair_table(html)
        self.assertFalse(retry)
        self.assertEqual(len(elements), 1)
        self.assertIn("nested_table_kept", issues)
        nested = BeautifulSoup(elements[0]["content"], "html.parser").find("table").find("table")
        self.assertIsNotNone(nested)
        combined = _visible_text(elements[0]["content"])
        for expected in ("Section", "A", "B", "1", "2", "Footer", "Value"):
            self.assertIn(expected, combined)

    def test_stacked_grids_with_distinct_widths_are_split(self):
        html = (
            "<table>"
            "<tr><td>A</td><td>B</td></tr>"
            "<tr><td>1</td><td>2</td></tr>"
            "<tr><td>C</td><td>D</td><td>E</td></tr>"
            "<tr><td>3</td><td>4</td><td>5</td></tr>"
            "</table>"
        )
        elements, retry, issues = table_validate.validate_and_repair_table(html)
        self.assertFalse(retry)
        self.assertEqual(len(elements), 2)
        self.assertIn("stacked_tables_split", issues)
        self.assertEqual(_visible_text(elements[0]["content"]), "A B 1 2")
        self.assertEqual(_visible_text(elements[1]["content"]), "C D E 3 4 5")

    def test_nested_table_quality_is_content_agnostic(self):
        html = "<table><tr><td><table><tr><td>X</td></tr></table></td></tr></table>"
        strict = table_validate.assess_table_quality(html)
        allowed = table_validate.assess_table_quality(html, allow_nested=True)
        self.assertNotIn("nested_table", strict["issues"])
        self.assertNotIn("nested_table", allowed["issues"])

    def test_quality_repair_accepts_only_lossless_structural_improvement(self):
        original = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "content": (
                        "<table><tr><td>Label</td><td>Value</td></tr>"
                        "<tr><td>Alpha</td></tr></table>"
                    ),
                }
            ],
        }
        repaired = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "content": (
                        "<table><tr><td>Label</td><td>Value</td></tr>"
                        "<tr><td>Alpha</td><td></td></tr></table>"
                    ),
                }
            ],
        }
        accepted, metrics = table_quality_repair.candidate_improvement(
            original, repaired
        )
        self.assertTrue(accepted)
        self.assertEqual(metrics["old_problem_tables"], 1)
        self.assertEqual(metrics["new_problem_tables"], 0)

        lossy = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "content": "<table><tr><td>Label</td><td></td></tr></table>",
                }
            ],
        }
        accepted, _ = table_quality_repair.candidate_improvement(original, lossy)
        self.assertFalse(accepted)

    def test_quality_repair_grafts_a_matched_table_without_dropping_the_page(self):
        broken = (
            "<table><tr><td>Category</td><td>Score</td></tr>"
            "<tr><td rowspan='2'>Group</td><td>Item One</td><td>90</td></tr>"
            "<tr><td>Item Two</td><td>80</td></tr></table>"
        )
        fixed = (
            "<table><tr><td colspan='2'>Category</td><td>Score</td></tr>"
            "<tr><td rowspan='2'>Group</td><td>Item One</td><td>90</td></tr>"
            "<tr><td>Item Two</td><td>80</td></tr></table>"
        )
        original = {
            "page_number": 1,
            "elements": [
                {"type": "text", "content": "Overview paragraph"},
                {
                    "type": "table",
                    "content": broken,
                    "caption": "Assessment summary",
                    "_source": "vlm_table_repaired",
                    "_confidence": 0.65,
                    "_issues": ["ragged_rows"],
                },
                {
                    "type": "table",
                    "content": (
                        "<table><tr><td>Key</td><td>Value</td></tr>"
                        "<tr><td>Alpha</td><td>Beta</td></tr></table>"
                    ),
                },
            ],
        }
        candidate = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "content": fixed,
                    "caption": "Assessment summary",
                }
            ],
        }

        grafted, grafts = table_quality_repair.graft_improved_tables(
            original, candidate
        )

        self.assertEqual(len(grafts), 1)
        self.assertEqual(len(grafted["elements"]), 3)
        self.assertEqual(grafted["elements"][0]["content"], "Overview paragraph")
        self.assertEqual(grafted["elements"][1]["content"], fixed)
        self.assertNotIn("_issues", grafted["elements"][1])
        self.assertEqual(
            grafted["elements"][2]["content"], original["elements"][2]["content"]
        )
        accepted, metrics = table_quality_repair.candidate_improvement(
            original, grafted
        )
        self.assertTrue(accepted)
        self.assertEqual(metrics["new_problem_tables"], 0)

    def test_quality_repair_does_not_graft_an_unrelated_table(self):
        original = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "content": (
                        "<table><tr><td>Category</td><td>Score</td></tr>"
                        "<tr><td rowspan='2'>Group</td><td>Item One</td><td>90</td></tr>"
                        "<tr><td>Item Two</td><td>80</td></tr></table>"
                    ),
                }
            ],
        }
        unrelated = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "content": (
                        "<table><tr><td>Region</td><td>Owner</td></tr>"
                        "<tr><td>North</td><td>Example</td></tr></table>"
                    ),
                }
            ],
        }

        grafted, grafts = table_quality_repair.graft_improved_tables(
            original, unrelated
        )

        self.assertEqual(grafts, [])
        self.assertEqual(grafted["elements"], original["elements"])

    def test_quality_repair_requires_stable_cell_order_without_grid_evidence(self):
        original = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "content": (
                        "<table><tr><td>A</td><td>B</td><td>C</td></tr>"
                        "<tr><td>Section</td><td>First item</td><td>10</td><td>X</td></tr>"
                        "<tr><td>Second item</td><td>20</td><td>Y</td><td></td></tr></table>"
                    ),
                }
            ],
        }
        reordered = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "content": (
                        "<table><tr><td>A</td><td>B</td><td>C</td><td></td></tr>"
                        "<tr><td>Section</td><td>Second item</td><td>20</td><td>Y</td></tr>"
                        "<tr><td>First item</td><td>10</td><td>X</td><td></td></tr></table>"
                    ),
                }
            ],
        }

        unchanged, strict_grafts = table_quality_repair.graft_improved_tables(
            original, reordered
        )
        corrected, evidenced_grafts = table_quality_repair.graft_improved_tables(
            original, reordered, min_sequence_similarity=0.50
        )
        _, strict_metrics = table_quality_repair.candidate_improvement(
            original, reordered
        )
        _, evidenced_metrics = table_quality_repair.candidate_improvement(
            original, reordered, min_table_sequence_similarity=0.50
        )

        self.assertEqual(strict_grafts, [])
        self.assertEqual(unchanged, original)
        self.assertEqual(len(evidenced_grafts), 1)
        self.assertNotEqual(corrected, original)
        self.assertFalse(strict_metrics["table_sequence_preserved"])
        self.assertTrue(evidenced_metrics["table_sequence_preserved"])

    def test_quality_repair_requires_distinct_matches_for_multiple_tables(self):
        broken = {
            "type": "table",
            "content": (
                "<table><tr><td>Label</td><td>Value</td></tr>"
                "<tr><td rowspan='2'>Group</td><td>First</td><td>10</td></tr>"
                "<tr><td>Second</td><td>20</td></tr></table>"
            ),
        }
        repaired = {
            "type": "table",
            "content": (
                "<table><tr><td colspan='2'>Label</td><td>Value</td></tr>"
                "<tr><td rowspan='2'>Group</td><td>First</td><td>10</td></tr>"
                "<tr><td>Second</td><td>20</td></tr></table>"
            ),
        }
        original = {"elements": [broken, broken]}
        candidate = {"elements": [repaired]}

        preserved = table_quality_repair._problem_table_sequence_preserved(
            original, candidate, minimum_similarity=0.98
        )

        self.assertFalse(preserved)

    def test_quality_repair_rejects_page_content_reordering(self):
        broken = (
            "<table><tr><td>Category</td><td>Score</td></tr>"
            "<tr><td rowspan='2'>Group</td><td>Item One</td><td>90</td></tr>"
            "<tr><td>Item Two</td><td>80</td></tr></table>"
        )
        fixed = (
            "<table><tr><td colspan='2'>Category</td><td>Score</td></tr>"
            "<tr><td rowspan='2'>Group</td><td>Item One</td><td>90</td></tr>"
            "<tr><td>Item Two</td><td>80</td></tr></table>"
        )
        first = "Alpha section contains a long and distinct opening explanation."
        last = "Omega section contains a separate and equally distinct conclusion."
        original = {
            "elements": [
                {"type": "text", "content": first},
                {"type": "table", "content": broken},
                {"type": "text", "content": last},
            ]
        }
        reordered = {
            "elements": [
                {"type": "text", "content": last},
                {"type": "table", "content": fixed},
                {"type": "text", "content": first},
            ]
        }

        accepted, metrics = table_quality_repair.candidate_improvement(
            original, reordered
        )

        self.assertFalse(accepted)
        self.assertLess(metrics["order_similarity"], 0.80)


if __name__ == "__main__":
    unittest.main()
