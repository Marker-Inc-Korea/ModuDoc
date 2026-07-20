from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw

import layout_consistency_repair as repair


class LayoutConsistencyRepairTests(unittest.TestCase):
    def test_panel_candidate_requires_dense_raster_and_a_text_run(self):
        data = {
            "elements": [
                {"type": "heading_1", "content": "Features"},
                {"type": "text", "content": "First panel"},
                {"type": "text", "content": "Second panel"},
                {"type": "text", "content": "Third panel"},
                {"type": "text", "content": "Fourth panel"},
            ]
        }
        image = Image.new("RGB", (600, 600), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((40, 40, 560, 560), fill=(170, 170, 170))

        with tempfile.TemporaryDirectory() as directory:
            dense = Path(directory) / "dense.png"
            sparse = Path(directory) / "sparse.png"
            image.save(dense)
            Image.new("RGB", (600, 600), "white").save(sparse)
            dense_score = repair._candidate_score(dense, data)
            sparse_score = repair._candidate_score(sparse, data)

        self.assertIsNotNone(dense_score)
        self.assertIsNone(sparse_score)

    def test_reassignment_gate_accepts_only_text_inventory_movement(self):
        original = {
            "page_number": 1,
            "elements": [
                {"type": "heading_1", "content": "Features"},
                {
                    "type": "text",
                    "content": "Search\nFirst bullet",
                    "_source": "vlm_page",
                    "_confidence": 0.91,
                },
                {"type": "text", "content": "Privacy\nSecond bullet\nSearch options"},
                {
                    "type": "table",
                    "content": "<table><tr><td>Old</td><td>New</td></tr></table>",
                },
            ],
        }
        corrected = {
            "page_number": 1,
            "elements": [
                {"type": "heading_1", "content": "Features"},
                {
                    "type": "text",
                    "content": "Search\nSearch options\nFirst bullet",
                },
                {"type": "text", "content": "Privacy\nSecond bullet"},
                original["elements"][3].copy(),
            ],
        }

        self.assertTrue(repair._reassignment_only(original, corrected))

        changed_table = {
            **corrected,
            "elements": corrected["elements"][:-1]
            + [{"type": "table", "content": "<table><tr><td>Changed</td></tr></table>"}],
        }
        self.assertFalse(repair._reassignment_only(original, changed_table))

        missing_text = {
            **corrected,
            "elements": [dict(item) for item in corrected["elements"]],
        }
        missing_text["elements"][1]["content"] = "Search\nFirst bullet"
        self.assertFalse(repair._reassignment_only(original, missing_text))

        scrambled = {
            **corrected,
            "elements": [dict(item) for item in corrected["elements"]],
        }
        scrambled["elements"][1]["content"] = "Search\noptions Search\nFirst bullet"
        scrambled["elements"][2]["content"] = "Privacy\nSecond bullet"
        self.assertFalse(repair._reassignment_only(original, scrambled))

        merged = repair._merge_reassignment(original, corrected)
        self.assertIsNotNone(merged)
        self.assertEqual(merged["elements"][1]["_source"], "vlm_page")
        self.assertEqual(merged["elements"][1]["_confidence"], 0.91)
        self.assertEqual(
            merged["elements"][1]["content"],
            "Search\nSearch options\nFirst bullet",
        )

        invalid_element = {
            **corrected,
            "elements": corrected["elements"][:-1] + ["not-an-element"],
        }
        self.assertFalse(repair._reassignment_only(original, invalid_element))


if __name__ == "__main__":
    unittest.main()
