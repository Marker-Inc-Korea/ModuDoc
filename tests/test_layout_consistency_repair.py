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

    def test_reassignment_gate_allows_bullet_segments_to_move_between_panels(self):
        original = {
            "elements": [
                {"type": "text", "content": "Search"},
                {
                    "type": "text",
                    "content": (
                        "Privacy • Private period • Search options "
                        "• Records lookup"
                    ),
                },
            ]
        }
        corrected = {
            "elements": [
                {
                    "type": "text",
                    "content": "Search\n• Search options\n• Records lookup",
                },
                {"type": "text", "content": "Privacy\n• Private period"},
            ]
        }

        self.assertTrue(repair._reassignment_only(original, corrected))

    def test_merge_restores_original_bullet_spelling_after_vlm_reformatting(self):
        original = {
            "elements": [
                {
                    "type": "text",
                    "content": "Search\n• Query",
                    "_source": "vlm_page",
                },
                {
                    "type": "text",
                    "content": "Privacy\n• Shield\n• History",
                },
            ]
        }
        reformatted = {
            "elements": [
                {"type": "text", "content": "Search\n- Query\n- History"},
                {"type": "text", "content": "Privacy\n- Shield"},
            ]
        }

        merged = repair._merge_reassignment(original, reformatted)

        self.assertIsNotNone(merged)
        self.assertEqual(
            merged["elements"][0]["content"],
            "Search\n• Query\n• History",
        )
        self.assertEqual(merged["elements"][0]["_source"], "vlm_page")

    def test_merge_rejects_rewritten_bullet_text_during_canonicalization(self):
        original = {
            "elements": [
                {"type": "text", "content": "Search\n• Query"},
                {"type": "text", "content": "Privacy\n• History"},
            ]
        }
        rewritten = {
            "elements": [
                {"type": "text", "content": "Search\n- Queries"},
                {"type": "text", "content": "Privacy\n- History"},
            ]
        }

        self.assertIsNone(repair._merge_reassignment(original, rewritten))

    def test_merge_restores_one_near_typo_while_moving_the_exact_source(self):
        original = {
            "elements": [
                {
                    "type": "text",
                    "content": "Search panel\n• Search condition convenience improved",
                },
                {"type": "text", "content": "Privacy panel\n• Private period"},
            ]
        }
        candidate = {
            "elements": [
                {"type": "text", "content": "Search panel"},
                {
                    "type": "text",
                    "content": (
                        "Privacy panel\n- Private period\n"
                        "- Search condition convenience improves"
                    ),
                },
            ]
        }

        merged = repair._merge_reassignment(original, candidate)

        self.assertIsNotNone(merged)
        self.assertEqual(
            merged["elements"][1]["content"],
            "Privacy panel\n• Private period\n• Search condition convenience improved",
        )

    def test_merge_rejects_a_semantically_different_single_token_rewrite(self):
        original = {
            "elements": [
                {
                    "type": "text",
                    "content": "Search panel\n• Search condition convenience improved",
                },
                {"type": "text", "content": "Privacy panel\n• Private period"},
            ]
        }
        rewritten = {
            "elements": [
                {"type": "text", "content": "Search panel"},
                {
                    "type": "text",
                    "content": (
                        "Privacy panel\n- Private period\n"
                        "- Search condition convenience pricing"
                    ),
                },
            ]
        }

        self.assertIsNone(repair._merge_reassignment(original, rewritten))

    def test_public_candidate_has_authoritative_element_indexes(self):
        candidate = repair._public_candidate(
            {
                "page_number": 4,
                "elements": [
                    {"type": "heading_1", "content": "Features"},
                    {"type": "text", "content": "Search\n• Query"},
                ],
            }
        )

        self.assertEqual(candidate["page_number"], 4)
        self.assertEqual(
            [element["index"] for element in candidate["elements"]], [0, 1]
        )

    def test_grounded_final_review_maps_every_changed_panel(self):
        original = {
            "elements": [
                {"type": "heading_1", "content": "Features"},
                {"type": "text", "content": "Search"},
                {
                    "type": "text",
                    "content": "Privacy\n• Private period\n• Query options",
                },
            ]
        }
        candidate = {
            "elements": [
                {"type": "heading_1", "content": "Features"},
                {"type": "text", "content": "Search\n• Query options"},
                {"type": "text", "content": "Privacy\n• Private period"},
            ]
        }
        changed = repair._changed_text_indexes(original, candidate)
        verdict = {
            "pass": True,
            "reading_order_matches": True,
            "changed_elements": [
                {
                    "candidate_index": 1,
                    "visible_card_title": "Search",
                    "image_row": 1,
                    "image_column": 1,
                    "content_matches_visible_card": True,
                    "evidence": "The query-options bullet is inside the Search card.",
                },
                {
                    "candidate_index": 2,
                    "visible_card_title": "Privacy",
                    "image_row": 1,
                    "image_column": 2,
                    "content_matches_visible_card": True,
                    "evidence": "The private-period bullet is inside the Privacy card.",
                },
            ],
        }

        self.assertEqual(changed, [1, 2])
        self.assertTrue(repair._final_review_accepts(verdict, candidate, changed))

        missing_mapping = {
            **verdict,
            "changed_elements": verdict["changed_elements"][:1],
        }
        self.assertFalse(
            repair._final_review_accepts(missing_mapping, candidate, changed)
        )

        wrong_title = {
            **verdict,
            "changed_elements": [
                {**verdict["changed_elements"][0], "visible_card_title": "Privacy"},
                verdict["changed_elements"][1],
            ],
        }
        self.assertFalse(repair._final_review_accepts(wrong_title, candidate, changed))

        reversed_visual_order = {
            **verdict,
            "changed_elements": [
                {**verdict["changed_elements"][0], "image_column": 2},
                {**verdict["changed_elements"][1], "image_column": 1},
            ],
        }
        self.assertFalse(
            repair._final_review_accepts(reversed_visual_order, candidate, changed)
        )

        ungrounded = {
            **verdict,
            "changed_elements": [
                {**verdict["changed_elements"][0], "evidence": ""},
                verdict["changed_elements"][1],
            ],
        }
        self.assertFalse(repair._final_review_accepts(ungrounded, candidate, changed))


if __name__ == "__main__":
    unittest.main()
