import unittest

import zoom_tables


class ZoomTableSafetyTests(unittest.TestCase):
    def test_repeated_cell_loss_is_not_treated_as_lossless(self):
        original = (
            "<table><tr><td>Region</td><td>Short</td><td>Medium</td><td>Long</td></tr>"
            "<tr><td>North</td><td>YES</td><td>YES</td><td>YES</td></tr></table>"
        )
        missing_repeat = (
            "<table><tr><td>Region</td><td>Short</td><td>Medium</td><td>Long</td></tr>"
            "<tr><td>North</td><td>YES</td><td>YES</td><td></td></tr></table>"
        )

        self.assertFalse(
            zoom_tables._table_text_preserved(
                original, missing_repeat, threshold=0.995
            )
        )

    def test_assignment_rejects_a_candidate_that_merges_neighboring_tables(self):
        elements = [
            {
                "type": "table",
                "content": "<table><tr><td>Item</td><td>Value</td></tr></table>",
            },
            {"type": "heading_2", "content": "Independent section"},
            {
                "type": "table",
                "content": "<table><tr><td>Region</td><td>Owner</td></tr></table>",
            },
        ]
        candidates = [
            {
                "type": "table",
                "content": (
                    "<table><tr><td>Item</td><td>Value</td>"
                    "<td>Region</td><td>Owner</td></tr></table>"
                ),
            }
        ]

        assignment = zoom_tables._lossless_table_assignment(
            elements, [0, 2], candidates
        )

        self.assertIsNone(assignment)

    def test_assignment_accepts_a_span_only_correction(self):
        elements = [
            {
                "type": "table",
                "content": (
                    "<table><tr><td>Group</td><td>Value</td></tr>"
                    "<tr><td>A</td><td>B</td><td>C</td></tr></table>"
                ),
            }
        ]
        candidates = [
            {
                "type": "table",
                "content": (
                    "<table><tr><td colspan='2'>Group</td><td>Value</td></tr>"
                    "<tr><td>A</td><td>B</td><td>C</td></tr></table>"
                ),
            }
        ]

        assignment = zoom_tables._lossless_table_assignment(
            elements, [0], candidates
        )

        self.assertEqual(assignment, {0: 0})

    def test_assignment_rejects_reordered_cell_text(self):
        elements = [
            {
                "type": "table",
                "content": (
                    "<table><tr><td>First</td><td>10</td></tr>"
                    "<tr><td>Second</td><td>20</td></tr></table>"
                ),
            }
        ]
        candidates = [
            {
                "type": "table",
                "content": (
                    "<table><tr><td>Second</td><td>20</td></tr>"
                    "<tr><td>First</td><td>10</td></tr></table>"
                ),
            }
        ]

        assignment = zoom_tables._lossless_table_assignment(
            elements, [0], candidates
        )

        self.assertIsNone(assignment)

    def test_page_selection_rejects_identical_matches_on_multiple_pages(self):
        source = {
            "type": "table",
            "content": (
                "<table><tr><td>Label</td><td>Value</td></tr>"
                "<tr><td>Repeated metric</td><td>10</td></tr></table>"
            ),
        }
        candidate = {"type": "table", "content": source["content"]}
        pages = ["page-a", "page-b"]
        pdata = {
            "page-a": {"elements": [source]},
            "page-b": {"elements": [source.copy()]},
        }
        zoom_cells = zoom_tables._cellset(candidate["content"])

        selected = zoom_tables._select_unique_table_page(
            pdata, pages, zoom_cells, [candidate]
        )

        self.assertIsNone(selected)

    def test_page_selection_keeps_one_lossless_match(self):
        source = {
            "type": "table",
            "content": (
                "<table><tr><td>Label</td><td>Value</td></tr>"
                "<tr><td>Repeated metric</td><td>10</td></tr></table>"
            ),
        }
        unrelated = {
            "type": "table",
            "content": (
                "<table><tr><td>Label</td><td>Value</td></tr>"
                "<tr><td>Different metric</td><td>20</td></tr></table>"
            ),
        }
        candidate = {"type": "table", "content": source["content"]}
        pages = ["page-a", "page-b"]
        pdata = {
            "page-a": {"elements": [source]},
            "page-b": {"elements": [unrelated]},
        }
        zoom_cells = zoom_tables._cellset(candidate["content"])

        selected = zoom_tables._select_unique_table_page(
            pdata, pages, zoom_cells, [candidate]
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected[0], "page-a")


if __name__ == "__main__":
    unittest.main()
