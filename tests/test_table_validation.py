import unittest

from bs4 import BeautifulSoup

import table_validate
import table_quality_repair


def _visible_text(html):
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


class GenericTableRepairTests(unittest.TestCase):
    def test_nested_table_is_promoted_without_losing_source_text(self):
        html = (
            "<table>"
            "<tr><td>Section</td><td><table><tr><td>A</td><td>B</td></tr>"
            "<tr><td>1</td><td>2</td></tr></table></td></tr>"
            "<tr><td>Footer</td><td>Value</td></tr>"
            "</table>"
        )
        elements, retry, issues = table_validate.validate_and_repair_table(html)
        self.assertFalse(retry)
        self.assertEqual(len(elements), 2)
        self.assertIn("nested_table_split", issues)
        combined = " ".join(_visible_text(item["content"]) for item in elements)
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
        self.assertIn("nested_table", strict["issues"])
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


if __name__ == "__main__":
    unittest.main()
