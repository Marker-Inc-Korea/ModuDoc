from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from PIL import Image, ImageDraw

import table_validate
import table_quality_repair


def _visible_text(html):
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


class GenericTableRepairTests(unittest.TestCase):
    @staticmethod
    def _grouped_stub_table():
        def row(cells, tag="td"):
            return "<tr>" + "".join(
                f"<{tag}>{value}</{tag}>" for value in cells
            ) + "</tr>"

        header = row(
            ["Category", "Measure", "A", "B", "C", "D", "Result"],
            "th",
        )
        body = []
        for group in ("North<br>Zone", "South<br>Zone"):
            body.extend(
                [
                    row([group, "Metric one", "1", "2", "3", "4", "5", "6"]),
                    row(["Metric two", "7", "8", "9", "10", "11", "12"]),
                    row(["Metric three", "", "13", "14", "15", "16", "17", "18"]),
                    row(["Metric four", "19", "20", "21", "22", "23", "24"]),
                ]
            )
        summary = row(["Grand total", "", "25", "26", "27", "28", "29", ""])
        return "<table>" + header + "".join(body) + summary + "</table>"

    @staticmethod
    def _rowspan_section_table():
        return (
            "<table>"
            "<tr><th>Kind</th><th>Terms</th><th>Coverage</th></tr>"
            "<tr><td rowspan='3'>Primary group</td><td>Build phase</td>"
            "<td>Inset summary with enough words to be longer</td></tr>"
            "<tr><td>Main conditions</td>"
            "<td rowspan='2'>Primary coverage statement</td></tr>"
            "<tr><td><table><tr><td>A</td><td>One</td></tr>"
            "<tr><td>B</td><td>Two</td></tr></table></td></tr>"
            "<tr><td>Second group</td><td>Second terms</td>"
            "<td>Second coverage</td></tr>"
            "<tr><td>Third group</td><td>Third terms</td>"
            "<td>Third coverage</td></tr>"
            "</table>"
        )

    @staticmethod
    def _rowspan_section_image(full_width_section=True, right_internal_box=True):
        image = Image.new("L", (1000, 1100), "white")
        draw = ImageDraw.Draw(image)
        columns = [100, 330, 700, 900]
        rows = [100, 190, 270, 700, 880, 1000]
        for y in rows:
            draw.line((columns[0], y, columns[-1], y), fill="black", width=3)
        for row_index, (top, bottom) in enumerate(zip(rows, rows[1:])):
            draw.line(
                (columns[0], top, columns[0], bottom), fill="black", width=3
            )
            draw.line(
                (columns[-1], top, columns[-1], bottom), fill="black", width=3
            )
            if row_index != 1 or not full_width_section:
                for x in columns[1:-1]:
                    draw.line((x, top, x, bottom), fill="black", width=3)

        # Short internal lines stay well inside one outer cell.
        for x in (390, 480, 650):
            draw.line((x, 430, x, 620), fill="black", width=3)
        for y in (430, 520, 620):
            draw.line((390, y, 650, y), fill="black", width=3)
        if right_internal_box:
            for y in (380, 560):
                draw.line((720, y, 880, y), fill="black", width=3)
        return image

    @staticmethod
    def _flattened_section_table():
        return (
            "<table>"
            "<tr><th>Kind</th><th>Terms</th><th>Coverage</th></tr>"
            "<tr><td rowspan='3'>Primary group</td>"
            "<td colspan='2'>Build phase</td></tr>"
            "<tr><td rowspan='2'>Main conditions</td>"
            "<td>Primary coverage heading</td></tr>"
            "<tr><td>(A)<br>First detail<br>(B)<br>Second detail</td>"
            "<td>Coverage continuation</td></tr>"
            "<tr><td>Second group</td><td colspan='2'>Second terms</td>"
            "<td>Second coverage</td></tr>"
            "<tr><td>Third group</td>"
            "<td colspan='2'>Third terms<br>Shared trailing detail</td>"
            "<td>Shared trailing detail</td></tr>"
            "</table>"
        )

    @staticmethod
    def _nested_table_with_external_note_image():
        image = Image.new("L", (1000, 1000), "white")
        draw = ImageDraw.Draw(image)
        rows = [100, 200, 300, 400, 500, 800]
        for y in rows:
            draw.line((100, y, 900, y), fill="black", width=3)
        for top, bottom in zip(rows[:3], rows[1:4]):
            for x in (300, 500, 700):
                draw.line((x, top, x, bottom), fill="black", width=3)
        draw.line((300, rows[3], 300, rows[4]), fill="black", width=3)
        draw.line((300, rows[4], 300, rows[5]), fill="black", width=3)
        for x in (400, 500, 600, 700, 800):
            draw.line((x, 560, x, 740), fill="black", width=3)
        for y in (560, 620, 680, 740):
            draw.line((300, y, 900, y), fill="black", width=3)
        return image

    def test_grouped_stub_rowspans_are_restored_without_text_changes(self):
        original = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "content": self._grouped_stub_table(),
                    "_source": "vlm",
                    "_confidence": 0.6,
                    "_issues": ["ragged_rows"],
                }
            ],
        }

        repaired, changes = table_quality_repair._normalize_grouped_stub_rowspans(
            original
        )

        self.assertIsNotNone(repaired)
        self.assertEqual(changes[0]["strategy"], "grouped_stub_rowspans_restored")
        repaired_table = repaired["elements"][0]
        self.assertEqual(
            table_quality_repair._table_geometry(repaired_table)[
                "expanded_row_widths"
            ],
            [8] * 10,
        )
        soup = BeautifulSoup(repaired_table["content"], "html.parser")
        self.assertEqual(soup.find("th").get("colspan"), "2")
        self.assertEqual(
            [cell.get("rowspan") for cell in soup.find_all("td") if cell.get("rowspan")],
            ["4", "4"],
        )
        self.assertEqual(
            table_quality_repair._page_visible_text(original),
            table_quality_repair._page_visible_text(repaired),
        )
        self.assertEqual(table_quality_repair._problem_tables(repaired), [])

    def test_grouped_stub_repair_rejects_irregular_group_boundaries(self):
        html = self._grouped_stub_table().replace(
            "South<br/>Zone", "South Zone", 1
        ).replace("South<br>Zone", "South Zone", 1)
        original = {"elements": [{"type": "table", "content": html}]}

        repaired, changes = table_quality_repair._normalize_grouped_stub_rowspans(
            original
        )

        self.assertIsNone(repaired)
        self.assertEqual(changes, [])

    def test_nested_vlm_table_is_selected_for_visual_review(self):
        html = (
            "<table><tr><td colspan='2'>Phase</td></tr>"
            "<tr><td>Plan</td><td><table><tr><td>A</td><td>B</td></tr>"
            "<tr><td>1</td><td>2</td></tr></table></td></tr></table>"
        )
        source = {
            "elements": [
                {
                    "type": "table",
                    "content": html,
                    "_source": "vlm_table_repaired",
                    "_issues": ["nested_table_kept"],
                }
            ]
        }

        problems = table_quality_repair._problem_tables(source)

        self.assertEqual(len(problems), 1)
        self.assertIn("possible_nested_layout_mismatch", problems[0]["issues"])

        preview = table_quality_repair._preview_tables(
            {"elements": [{"type": "table", "content": html}]}
        )
        self.assertEqual(table_quality_repair._problem_tables(preview), [])

    def test_only_nested_layout_mismatch_requires_independent_review(self):
        self.assertTrue(
            table_quality_repair._needs_nested_layout_review(
                [{"issues": ["possible_nested_layout_mismatch"]}]
            )
        )
        self.assertFalse(
            table_quality_repair._needs_nested_layout_review(
                [{"issues": ["possible_cross_row_bleed"]}]
            )
        )

    def test_table_topology_signature_ignores_text_and_header_tags(self):
        first = {
            "content": (
                "<table><tr><th colspan='2'>Header</th></tr>"
                "<tr><td>Left</td><td>Right</td></tr></table>"
            )
        }
        second = {
            "content": (
                "<table><tr><td colspan='2'>Different</td></tr>"
                "<tr><td>One</td><td>Two</td></tr></table>"
            )
        }

        self.assertEqual(
            table_quality_repair._table_topology_signature(first),
            table_quality_repair._table_topology_signature(second),
        )

    def test_metadata_only_nested_repair_is_not_an_improvement(self):
        html = (
            "<table><tr><td>Area</td><td>Detail</td></tr>"
            "<tr><td>First</td><td><table><tr><td>A</td></tr>"
            "</table></td></tr></table>"
        )
        original = {
            "elements": [
                {
                    "type": "table",
                    "content": html,
                    "_source": "vlm_table_repaired",
                    "_issues": ["nested_table_kept"],
                }
            ]
        }
        metadata_stripped = {
            "elements": [{"type": "table", "content": html}]
        }

        accepted, metrics = table_quality_repair.candidate_improvement(
            original, metadata_stripped, min_table_sequence_similarity=0.50
        )

        self.assertFalse(accepted)
        self.assertFalse(metrics["nested_layout_topology_changed"])

    def test_nested_parent_cell_move_changes_topology(self):
        inner = "<table><tr><td>A</td></tr></table>"
        original = {
            "elements": [
                {
                    "type": "table",
                    "content": (
                        "<table><tr><td>Group</td><td>Detail</td>"
                        f"<td>{inner}</td></tr></table>"
                    ),
                    "_source": "vlm_table_repaired",
                    "_issues": ["nested_table_kept"],
                }
            ]
        }
        moved = {
            "elements": [
                {
                    "type": "table",
                    "content": (
                        f"<table><tr><td>Group</td><td>{inner}</td>"
                        "<td>Detail</td></tr></table>"
                    ),
                }
            ]
        }

        self.assertTrue(
            table_quality_repair._nested_layout_topology_changed(original, moved)
        )

    def test_nested_layout_review_uses_all_image_bands_and_full_budget(self):
        candidate = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "content": (
                        "<table><tr><td>Area</td><td>Detail</td></tr>"
                        "<tr><td>First</td><td><table><tr><td>A</td></tr>"
                        "</table></td></tr></table>"
                    ),
                }
            ],
        }
        with (
            patch.object(
                table_quality_repair,
                "_encode_table_images",
                return_value=["overview", "upper", "lower"],
            ),
            patch.object(
                table_quality_repair,
                "_request_json",
                return_value={"pass": True, "outer_columns": 2},
            ) as request_json,
        ):
            verdict = table_quality_repair._request_nested_layout_review(
                object(), "test-model", Path("unused.png"), candidate
            )

        self.assertTrue(verdict["pass"])
        request = request_json.call_args.args[1]
        content = request["messages"][1]["content"]
        self.assertEqual(
            [item["image_url"]["url"] for item in content[:-1]],
            [
                "data:image/png;base64,overview",
                "data:image/png;base64,upper",
                "data:image/png;base64,lower",
            ],
        )
        self.assertEqual(
            request["max_tokens"],
            table_quality_repair.TABLE_QUALITY_REPAIR_MAX_TOKENS,
        )
        self.assertIn("outer-row boundary", request["messages"][0]["content"])

    def test_nested_layout_review_must_match_parsed_outer_geometry(self):
        inner = (
            "<table><tr><td>A</td><td>B</td></tr>"
            "<tr><td>1</td><td>2</td></tr></table>"
        )
        candidate = {
            "elements": [
                {
                    "type": "table",
                    "content": (
                        "<table>"
                        "<tr><td colspan='3'>Header</td></tr>"
                        f"<tr><td>Group</td><td>{inner}</td><td>Note</td></tr>"
                        "<tr><td>Next</td><td>Value</td><td>Tail</td></tr>"
                        "<tr><td>Last</td><td>Value</td><td>Tail</td></tr>"
                        "</table>"
                    ),
                }
            ]
        }

        self.assertTrue(
            table_quality_repair._nested_review_geometry_consistent(
                candidate, {"outer_rows": 4, "outer_columns": 3}
            )
        )
        self.assertFalse(
            table_quality_repair._nested_review_geometry_consistent(
                candidate, {"outer_rows": 6, "outer_columns": 3}
            )
        )

    def test_nested_layout_review_rejects_flattened_inner_rows(self):
        candidate = {
            "elements": [
                {
                    "type": "table",
                    "content": (
                        "<table>"
                        "<tr><td colspan='3'>Header</td></tr>"
                        "<tr><td>Group</td><td>Detail</td><td>Note</td></tr>"
                        "<tr><td>A</td><td>B</td><td>C</td><td>D</td></tr>"
                        "<tr><td>1</td><td>2</td><td>3</td></tr>"
                        "<tr><td>Next</td><td>Value</td><td>Tail</td></tr>"
                        "<tr><td>Last</td><td>Value</td><td>Tail</td></tr>"
                        "</table>"
                    ),
                }
            ]
        }
        review = {"outer_rows": 4, "outer_columns": 3}

        self.assertFalse(
            table_quality_repair._nested_review_geometry_consistent(
                candidate, review
            )
        )
        feedback = table_quality_repair._nested_review_geometry_feedback(
            candidate, review
        )
        self.assertIn("4 rows by 3 columns", feedback)
        self.assertIn("parent cell's nested_table", feedback)

    def test_rejected_layout_feedback_is_sent_to_the_next_repair_attempt(self):
        original = {
            "elements": [
                {
                    "type": "table",
                    "content": "<table><tr><td>Area</td><td>Detail</td></tr></table>",
                }
            ]
        }
        problems = [
            {"index": 0, "issues": ["possible_nested_layout_mismatch"]}
        ]
        with (
            patch.object(
                table_quality_repair,
                "_encode_table_images",
                return_value=["overview"],
            ),
            patch.object(
                table_quality_repair,
                "_request_json",
                return_value={"page_number": 1, "elements": []},
            ) as request_json,
        ):
            table_quality_repair._request_table_candidate(
                object(),
                "test-model",
                Path("unused.png"),
                original,
                problems,
                reviewer_feedback="A short inner line was treated as an outer row.",
            )

        request = request_json.call_args.args[1]
        user_text = request["messages"][1]["content"][-1]["text"]
        self.assertIn("independent layout reviewer rejected", user_text)
        self.assertIn("short inner line", user_text)

    def test_tall_table_encoding_adds_overlapping_detail_bands(self):
        with tempfile.TemporaryDirectory() as directory:
            portrait = Path(directory) / "portrait.png"
            landscape = Path(directory) / "landscape.png"
            Image.new("RGB", (600, 1200), "white").save(portrait)
            Image.new("RGB", (1200, 600), "white").save(landscape)

            with patch.object(table_quality_repair, "TABLE_QUALITY_REPAIR_CROPS", True):
                portrait_images = table_quality_repair._encode_table_images(portrait)
                landscape_images = table_quality_repair._encode_table_images(landscape)

        self.assertEqual(len(portrait_images), 3)
        self.assertEqual(len(set(portrait_images)), 2)
        self.assertEqual(len(landscape_images), 1)

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

    def test_recursive_table_tree_renders_nested_rows_without_flattening(self):
        tree = {
            "page_number": 1,
            "tables": [
                {
                    "caption": "Synthetic grid",
                    "rows": [
                        {
                            "cells": [
                                {"text": "A"},
                                {"text": "B"},
                                {"text": "C"},
                            ]
                        },
                        {"cells": [{"text": "Section", "colspan": 3}]},
                        {
                            "cells": [
                                {"text": "Left"},
                                {
                                    "text": "Detail",
                                    "nested_table": {
                                        "rows": [
                                            {
                                                "cells": [
                                                    {"text": "N1"},
                                                    {"text": "N2"},
                                                ]
                                            },
                                            {
                                                "cells": [
                                                    {"text": "1"},
                                                    {"text": "2"},
                                                ]
                                            },
                                        ]
                                    },
                                },
                                {"text": "Tail & note"},
                            ]
                        },
                    ],
                }
            ],
        }

        candidate = table_quality_repair._table_tree_candidate(tree)

        self.assertIsNotNone(candidate)
        element = candidate["elements"][0]
        self.assertEqual(element["caption"], "Synthetic grid")
        self.assertEqual(
            table_quality_repair._table_geometry(element)["expanded_row_widths"],
            [3, 3, 3],
        )
        soup = BeautifulSoup(element["content"], "html.parser")
        outer = soup.find("table")
        self.assertEqual(len(table_validate._rows_of(outer)), 3)
        self.assertEqual(len(outer.find("table").find_all("tr")), 2)
        self.assertIn("Tail &amp; note", element["content"])

    def test_recursive_table_tree_rejects_invalid_spans(self):
        tree = {
            "tables": [
                {
                    "rows": [
                        {"cells": [{"text": "A", "colspan": 0}]}
                    ]
                }
            ]
        }

        self.assertIsNone(table_quality_repair._table_tree_candidate(tree))

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

    def test_raster_evidenced_repair_merges_wrap_and_drops_phantom_column(self):
        top = (
            "<tr><td></td><td></td><td></td><td colspan='4'>Rating criteria</td>"
            "<td></td><td colspan='4'>Significance</td><td></td></tr>"
        )
        header = (
            "<tr>"
            + "".join(
                f"<td>{value}</td>"
                for value in (
                    "No.", "Topic", "Space", "Time", "Reversibility",
                    "Cumulative", "Residual", "Mobilization", "Build",
                    "Operate", "Close", "",
                )
            )
            + "</tr>"
        )
        continuation = (
            "<tr>" + "".join(
                f"<td>{'Effects' if index == 5 else ''}</td>"
                for index in range(12)
            ) + "</tr>"
        )
        body_rows = []
        for row_index in range(6):
            values = [
                str(row_index), f"Topic {row_index}", "L", "ST", "R", "",
                "", "0", "-1", "0", "0", "",
            ]
            if row_index == 0:
                values[1] = "NEGATIVE IMPACTS"
            body_rows.append(
                "<tr>" + "".join(f"<td>{value}</td>" for value in values) + "</tr>"
            )
        original = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "caption": "Impact summary",
                    "content": "<table>" + top + header + continuation + "".join(body_rows) + "</table>",
                }
            ],
        }
        image = Image.new("RGB", (1200, 900), "white")
        draw = ImageDraw.Draw(image)
        x_lines = [50 + 100 * index for index in range(12)]
        y_lines = [50 + 100 * index for index in range(9)]
        for y in y_lines:
            draw.line((x_lines[0], y, x_lines[-1], y), fill="black", width=3)
        for row_index, (upper, lower) in enumerate(zip(y_lines, y_lines[1:])):
            visible_x = (
                [x_lines[index] for index in (0, 1, 2, 3, 6, 7, 8, 10, 11)]
                if row_index == 0 else x_lines
            )
            for x in visible_x:
                draw.line((x, upper, x, lower), fill="black", width=3)

        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "grid.png"
            image.save(image_path)
            repaired, changes, metrics = (
                table_quality_repair._validated_deterministic_geometry_repair(
                    image_path, original
                )
            )

        self.assertIsNotNone(repaired)
        self.assertTrue(metrics["text_inventory_preserved"])
        self.assertEqual(metrics["new_problem_tables"], 0)
        self.assertEqual(len(changes), 2)
        table = repaired["elements"][0]
        self.assertIn("Cumulative<br/>Effects", table["content"])
        self.assertEqual(
            table_quality_repair._table_geometry(table)["expanded_row_widths"],
            [11] * 8,
        )

    def test_raster_evidenced_rowspan_section_group_is_rebuilt_losslessly(self):
        original = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "content": self._rowspan_section_table(),
                    "_source": "vlm_table_repaired",
                    "_confidence": 0.8,
                    "_issues": ["nested_table_kept"],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "grid.png"
            self._rowspan_section_image().save(image_path)
            repaired, changes, metrics = (
                table_quality_repair._validated_deterministic_geometry_repair(
                    image_path, original
                )
            )

        self.assertIsNotNone(repaired)
        self.assertTrue(metrics["text_inventory_preserved"])
        self.assertEqual(
            changes[0]["strategy"],
            "raster_rowspan_section_group_rebuilt",
        )
        element = repaired["elements"][0]
        self.assertEqual(
            table_quality_repair._table_geometry(element)["expanded_row_widths"],
            [3] * 5,
        )
        soup = BeautifulSoup(element["content"], "html.parser")
        rows = table_validate._rows_of(soup.find("table"))
        section_cells = rows[1].find_all(["td", "th"], recursive=False)
        self.assertEqual(len(section_cells), 1)
        self.assertEqual(section_cells[0].get("colspan"), "3")
        self.assertEqual(len(rows[2].find_all("table")), 2)
        self.assertEqual(table_quality_repair._problem_tables(repaired), [])
        self.assertCountEqual(
            table_quality_repair._page_visible_text(original),
            table_quality_repair._page_visible_text(repaired),
        )

    def test_flattened_section_group_is_rebuilt_from_unique_raster_grid(self):
        original = {
            "elements": [
                {
                    "type": "table",
                    "content": self._flattened_section_table(),
                    "_source": "vlm_table_repaired",
                    "_issues": ["ragged_rows"],
                }
            ]
        }
        source_text = (
            "Kind Terms Coverage Primary group Build phase Main conditions "
            "Primary coverage heading (A) First detail (B) Second detail "
            "Coverage continuation Second group Second terms Second coverage "
            "Third group Third terms Shared trailing detail"
        )
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "page.png"
            text_path = Path(directory) / "page.txt"
            self._rowspan_section_image().save(image_path)
            text_path.write_text(source_text, encoding="utf-8")

            repaired, changes, metrics = (
                table_quality_repair._validated_deterministic_geometry_repair(
                    image_path, original, text_path
                )
            )

        self.assertIsNotNone(repaired)
        self.assertEqual(
            changes[0]["strategy"],
            "raster_flattened_section_group_rebuilt",
        )
        self.assertEqual(changes[0]["source_supported_duplicates_removed"], 1)
        table = repaired["elements"][0]
        self.assertEqual(
            table_quality_repair._table_geometry(table)["expanded_row_widths"],
            [3] * 5,
        )
        self.assertEqual(
            len(BeautifulSoup(table["content"], "html.parser").find_all("table")),
            3,
        )
        self.assertTrue(metrics["source_supported_cleanup"])
        self.assertEqual(table_quality_repair._problem_tables(repaired), [])

    def test_flattened_section_rebuild_requires_internal_boxes_in_all_columns(self):
        original = {
            "elements": [
                {"type": "table", "content": self._flattened_section_table()}
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "page.png"
            self._rowspan_section_image(right_internal_box=False).save(image_path)
            repaired, changes = (
                table_quality_repair._raster_evidenced_flattened_section_rebuild(
                    image_path, original
                )
            )

        self.assertIsNone(repaired)
        self.assertEqual(changes, [])

    def test_nested_table_external_note_is_split_by_outer_raster_boundary(self):
        nested = (
            "<table>"
            + "".join(
                "<tr><td>A</td><td>B</td><td>C</td><td>D</td></tr>"
                for _ in range(4)
            )
            + "</table>"
        )
        html = (
            "<table>"
            "<tr><td>A</td><td>B</td><td>C</td><td>D</td></tr>"
            "<tr><td>E</td><td>F</td><td>G</td><td>H</td></tr>"
            "<tr><td>I</td><td>J</td><td>K</td><td>L</td></tr>"
            "<tr><td>M</td><td colspan='3'>Summary</td></tr>"
            f"<tr><td rowspan='2'>Detail</td><td colspan='5'>{nested}</td></tr>"
            "<tr><td colspan='6'>Outside note</td></tr>"
            "</table>"
        )
        original = {
            "elements": [
                {
                    "type": "table",
                    "content": html,
                    "_source": "vlm_table_repaired",
                    "_issues": ["nested_table_kept", "ragged_rows"],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "page.png"
            self._nested_table_with_external_note_image().save(image_path)
            repaired, changes, metrics = (
                table_quality_repair._validated_deterministic_geometry_repair(
                    image_path, original
                )
            )

        self.assertIsNotNone(repaired)
        self.assertEqual(changes[0]["strategy"], "raster_external_note_extracted")
        self.assertEqual(
            [element["type"] for element in repaired["elements"]],
            ["table", "text"],
        )
        self.assertEqual(
            table_quality_repair._table_geometry(repaired["elements"][0])[
                "expanded_row_widths"
            ],
            [4] * 5,
        )
        self.assertTrue(metrics["text_inventory_preserved"])
        self.assertEqual(table_quality_repair._problem_tables(repaired), [])

    def test_repeated_column_groups_are_rebuilt_only_with_source_copies(self):
        html = (
            "<table><tr><td>Before</td><td>After</td></tr>"
            + "".join(
                f"<tr><td>L{index}</td><td>R{index}</td></tr>"
                for index in range(1, 7)
            )
            + "</table>"
        )
        original = {"elements": [{"type": "table", "content": html}]}
        image = Image.new("L", (1000, 600), "white")
        draw = ImageDraw.Draw(image)
        xs = [100 + index * 133 for index in range(7)]
        xs[-1] = 900
        ys = [100, 200, 300, 400]
        for y in ys:
            draw.line((xs[0], y, xs[-1], y), fill="black", width=3)
        for x in xs:
            draw.line((x, ys[0], x, ys[-1]), fill="black", width=3)
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "page.png"
            text_path = Path(directory) / "page.txt"
            image.save(image_path)
            text_path.write_text(
                "Before After Before After Before After "
                + " ".join(f"L{i} R{i}" for i in range(1, 7)),
                encoding="utf-8",
            )
            repaired, changes, metrics = (
                table_quality_repair._validated_repeated_column_group_rebuild(
                    image_path, text_path, original
                )
            )
            text_path.write_text(
                "Before After " + " ".join(f"L{i} R{i}" for i in range(1, 7)),
                encoding="utf-8",
            )
            rejected = (
                table_quality_repair._validated_repeated_column_group_rebuild(
                    image_path, text_path, original
                )
            )
            ambiguous = {
                "elements": [
                    {
                        "type": "table",
                        "content": html.replace("Before", "No").replace(
                            "After", "ID"
                        ),
                    }
                ]
            }
            text_path.write_text(
                "No ID No ID No ID "
                + " ".join(f"L{i} R{i}" for i in range(1, 7)),
                encoding="utf-8",
            )
            ambiguous_rejected = (
                table_quality_repair._validated_repeated_column_group_rebuild(
                    image_path, text_path, ambiguous
                )
            )

        self.assertIsNotNone(repaired)
        self.assertEqual(
            changes[0]["strategy"], "raster_repeated_column_groups_rebuilt"
        )
        self.assertEqual(
            table_quality_repair._table_geometry(repaired["elements"][0])[
                "expanded_row_widths"
            ],
            [6] * 3,
        )
        self.assertTrue(metrics["source_supported_header_repetitions"])
        self.assertIsNone(rejected[0])
        self.assertIsNone(ambiguous_rejected[0])

    def test_rowspan_section_rebuild_requires_a_raster_section_band(self):
        original = {
            "elements": [
                {
                    "type": "table",
                    "content": self._rowspan_section_table(),
                    "_source": "vlm_table_repaired",
                    "_issues": ["nested_table_kept"],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "grid.png"
            self._rowspan_section_image(full_width_section=False).save(image_path)
            repaired, changes = (
                table_quality_repair._raster_evidenced_rowspan_section_rebuild(
                    image_path, original
                )
            )

        self.assertIsNone(repaired)
        self.assertEqual(changes, [])

    def test_rowspan_section_rebuild_requires_each_internal_box(self):
        original = {
            "elements": [
                {
                    "type": "table",
                    "content": self._rowspan_section_table(),
                    "_source": "vlm_table_repaired",
                    "_issues": ["nested_table_kept"],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "grid.png"
            self._rowspan_section_image(right_internal_box=False).save(image_path)
            repaired, changes = (
                table_quality_repair._raster_evidenced_rowspan_section_rebuild(
                    image_path, original
                )
            )

        self.assertIsNone(repaired)
        self.assertEqual(changes, [])

    def test_raster_completes_one_deficient_grouped_header_colspan(self):
        original = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "content": (
                        "<table>"
                        "<tr><th rowspan='2'>Stub</th><th colspan='2'>Group A</th>"
                        "<th>Group B</th></tr>"
                        "<tr><th>A1</th><th>A2</th><th>B1</th><th>B2</th></tr>"
                        "<tr><td>R1</td><td>1</td><td>2</td><td>3</td><td>4</td></tr>"
                        "<tr><td>R2</td><td>5</td><td>6</td><td>7</td><td>8</td></tr>"
                        "<tr><td>R3</td><td>9</td><td>10</td><td>11</td><td>12</td></tr>"
                        "<tr><td>R4</td><td>13</td><td>14</td><td>15</td><td>16</td></tr>"
                        "</table>"
                    ),
                }
            ],
        }
        image = Image.new("RGB", (900, 640), "white")
        draw = ImageDraw.Draw(image)
        x_lines = [50, 210, 370, 530, 690, 850]
        full_y = [80, 240, 320, 400, 480, 560]
        for y in full_y:
            draw.line((x_lines[0], y, x_lines[-1], y), fill="black", width=3)
        draw.line((x_lines[1], 160, x_lines[-1], 160), fill="black", width=3)
        for x in (x_lines[0], x_lines[1], x_lines[3], x_lines[-1]):
            draw.line((x, full_y[0], x, 160), fill="black", width=3)
        for x in x_lines:
            draw.line((x, 160, x, full_y[-1]), fill="black", width=3)

        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "header-grid.png"
            image.save(image_path)
            repaired, changes, metrics = (
                table_quality_repair._validated_deterministic_geometry_repair(
                    image_path, original
                )
            )

        self.assertIsNotNone(repaired)
        self.assertEqual(changes[0]["strategy"], "raster_header_colspan_completed")
        self.assertTrue(metrics["text_inventory_preserved"])
        geometry = table_quality_repair._table_geometry(repaired["elements"][0])
        self.assertEqual(geometry["expanded_row_widths"], [5] * 6)
        soup = BeautifulSoup(repaired["elements"][0]["content"], "html.parser")
        first = table_validate._rows_of(soup.find("table"))[0]
        self.assertEqual(
            [table_validate._span_int(cell, "colspan") for cell in first.find_all(["td", "th"], recursive=False)],
            [1, 2, 2],
        )

    def test_raster_evidenced_repair_rebuilds_a_shifted_two_row_header(self):
        original = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "caption": "Summary grid",
                    "content": (
                        "<table>"
                        "<tr><td rowspan='2'>Code</td><td rowspan='2'>Subject</td>"
                        "<td colspan='3'>RATING GROUP</td>"
                        "<td rowspan='2'>Effect</td><td rowspan='2'>PHASE GROUP</td></tr>"
                        "<tr><td>Space</td><td>Time</td><td>Cumulative</td>"
                        "<td>Build</td><td>Operate</td></tr>"
                        "<tr><td>1</td><td>First item</td><td>L</td><td>ST</td>"
                        "<td></td><td>-1</td><td>0</td></tr>"
                        "<tr><td>2</td><td>Second item</td><td>L</td><td>LT</td>"
                        "<td></td><td>-2</td><td>0</td></tr>"
                        "<tr><td>3</td><td>Third item</td><td>L</td><td>ST</td>"
                        "<td></td><td>0</td><td>-1</td></tr>"
                        "</table>"
                    ),
                }
            ],
        }
        image = Image.new("RGB", (900, 680), "white")
        draw = ImageDraw.Draw(image)
        x_lines = [50 + 110 * index for index in range(8)]
        y_lines = [50 + 110 * index for index in range(6)]
        for y in y_lines:
            draw.line((x_lines[0], y, x_lines[-1], y), fill="black", width=3)
        for row_index, (upper, lower) in enumerate(zip(y_lines, y_lines[1:])):
            visible_x = (
                [x_lines[index] for index in (0, 1, 2, 3, 5, 6, 7)]
                if row_index == 0
                else x_lines
            )
            for x in visible_x:
                draw.line((x, upper, x, lower), fill="black", width=3)
        for start, end in ((3, 5), (6, 7)):
            draw.rectangle(
                (x_lines[start] + 20, y_lines[0] + 35, x_lines[end] - 20, y_lines[0] + 50),
                fill="black",
            )
        for column in range(7):
            draw.rectangle(
                (
                    x_lines[column] + 20,
                    y_lines[1] + 35,
                    x_lines[column] + 45,
                    y_lines[1] + 50,
                ),
                fill="black",
            )

        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "grid.png"
            image.save(image_path)
            repaired, changes, metrics = (
                table_quality_repair._validated_deterministic_geometry_repair(
                    image_path, original
                )
            )

        self.assertIsNotNone(repaired)
        self.assertEqual(changes[0]["strategy"], "raster_header_cells_rebuilt")
        self.assertTrue(metrics["text_inventory_preserved"])
        table = repaired["elements"][0]
        self.assertEqual(
            table_quality_repair._table_geometry(table)["expanded_row_widths"],
            [7] * 5,
        )
        soup = BeautifulSoup(table["content"], "html.parser")
        rows = table_validate._rows_of(soup.find("table"))
        first = rows[0].find_all(["td", "th"], recursive=False)
        second = rows[1].find_all(["td", "th"], recursive=False)
        self.assertEqual(
            [cell.get_text(" ", strip=True) for cell in first],
            ["", "", "", "RATING GROUP", "", "PHASE GROUP"],
        )
        self.assertEqual(
            [cell.get_text(" ", strip=True) for cell in second],
            ["Code", "Subject", "Space", "Time", "Cumulative Effect", "Build", "Operate"],
        )

    def test_borderless_formula_definitions_are_demoted_losslessly(self):
        original = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "content": (
                        "<table><tr><td>x<sub>i</sub>: item index</td><td>D<sub>i</sub>: item value</td></tr>"
                        "<tr><td>T</td><td>: total</td><td>t<sub>j</sub></td><td>: event date</td></tr>"
                        "<tr><td>P<sub>j</sub></td><td>: event amount</td></tr></table>"
                    ),
                    "caption": "Formula definitions",
                }
            ],
        }
        image = Image.new("RGB", (600, 400), "white")

        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "formula.png"
            image.save(image_path)
            demoted, records = (
                table_quality_repair._demote_borderless_definition_tables(
                    image_path, original
                )
            )
            validated, validated_records, metrics = (
                table_quality_repair._validated_borderless_definition_demotion(
                    image_path, original
                )
            )

        self.assertIsNotNone(demoted)
        self.assertEqual(records, [{"index": 0, "rows": 3}])
        self.assertEqual(demoted["elements"][0]["type"], "text")
        self.assertEqual(
            table_quality_repair._page_visible_text(original),
            table_quality_repair._page_visible_text(demoted),
        )
        self.assertEqual(table_quality_repair._problem_tables(demoted), [])
        self.assertEqual(validated, demoted | {"quality_repair": True})
        self.assertEqual(validated_records, records)
        self.assertEqual(metrics["old_problem_tables"], 1)
        self.assertEqual(metrics["new_problem_tables"], 0)

    def test_rectangular_formula_definitions_are_still_demoted(self):
        original = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "content": (
                        "<table><tr><td>x<sub>i</sub></td><td>: item index</td></tr>"
                        "<tr><td>D<sub>i</sub></td><td>: item value</td></tr>"
                        "<tr><td>T = ΣD<sub>i</sub></td><td>: total amount</td></tr>"
                        "</table>"
                    ),
                    "caption": "Formula definitions",
                }
            ],
        }
        image = Image.new("RGB", (600, 400), "white")

        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "formula.png"
            image.save(image_path)
            demoted, records, metrics = (
                table_quality_repair._validated_borderless_definition_demotion(
                    image_path, original
                )
            )

        self.assertIsNotNone(demoted)
        self.assertEqual(records, [{"index": 0, "rows": 3}])
        self.assertEqual(demoted["elements"][0]["type"], "text")
        self.assertEqual(metrics["old_problem_tables"], 0)
        self.assertEqual(metrics["new_problem_tables"], 0)
        self.assertEqual(metrics["old_table_count"], 1)
        self.assertEqual(metrics["new_table_count"], 0)

    def test_cross_row_label_bleed_is_sent_for_image_review(self):
        html = (
            "<table><tr><td>Category</td><td>Detail</td><td>Action</td></tr>"
            "<tr><td>First area</td><td>Initial risk</td>"
            "<td>A sufficiently long mitigation description ending Next area risk</td></tr>"
            "<tr><td>Next area</td><td>Secondary risk</td><td>Separate action</td></tr>"
            "</table>"
        )

        quality = table_validate.assess_table_quality(html)
        problems = table_quality_repair._problem_tables(
            {"elements": [{"type": "table", "content": html}]}
        )

        self.assertIn("possible_cross_row_bleed", quality["issues"])
        self.assertGreaterEqual(quality["confidence"], 0.75)
        self.assertEqual(problems[0]["issues"], ["possible_cross_row_bleed"])

    def test_near_duplicate_lines_inside_a_cell_are_sent_for_image_review(self):
        html = (
            "<table><tr><td>Category</td><td>Coverage</td></tr>"
            "<tr><td>Third party</td><td>Coverage for bodily injury and property and damage"
            "<br>Coverage for bodily injury and property damage</td></tr></table>"
        )

        quality = table_validate.assess_table_quality(html)
        problems = table_quality_repair._problem_tables(
            {"elements": [{"type": "table", "content": html}]}
        )

        self.assertIn("possible_internal_duplicate_text", quality["issues"])
        self.assertEqual(len(problems), 1)
        self.assertEqual(
            problems[0]["issues"], ["possible_internal_duplicate_text"]
        )

    def test_non_trailing_category_mention_is_not_a_bleed_signal(self):
        html = (
            "<table><tr><td>Category</td><td>Detail</td><td>Action</td></tr>"
            "<tr><td>First area</td><td>Initial risk</td>"
            "<td>Next area is discussed here, followed by a long independent conclusion</td></tr>"
            "<tr><td>Next area</td><td>Secondary risk</td><td>Separate action</td></tr>"
            "</table>"
        )

        quality = table_validate.assess_table_quality(html)

        self.assertNotIn("possible_cross_row_bleed", quality["issues"])

    def test_native_slice_uses_order_to_disambiguate_repeated_row_labels(self):
        native = (
            "<table><tr><th>Label</th><th>Value</th></tr>"
            "<tr><td>Repeated category</td><td>Early value</td></tr>"
            "<tr><td>Earlier section</td><td>Earlier detail</td></tr>"
            "<tr><td>Later first</td><td>One</td></tr>"
            "<tr><td>Later second</td><td>Two</td></tr>"
            "<tr><td>Repeated category</td><td>Later value</td></tr>"
            "<tr><td>Later third</td><td>Three</td></tr>"
            "<tr><td>Shared period</td><td></td></tr>"
            "<tr><td>Omitted middle row</td><td>Must survive</td></tr>"
            "<tr><td>Later fourth</td><td>Four</td></tr></table>"
        )
        page_fragment = (
            "<table><tr><th>Label</th><th>Value</th></tr>"
            "<tr><td></td><td>Leading continuation text</td></tr>"
            "<tr><td>Later first</td><td>One</td></tr>"
            "<tr><td>Later second</td><td>Two</td></tr>"
            "<tr><td>Repeated category</td><td>Later value</td></tr>"
            "<tr><td>Later third</td><td>Three</td></tr>"
            "<tr><td>Shared period</td><td></td></tr>"
            "<tr><td>Later fourth</td><td>Four</td></tr></table>"
        )

        sliced = table_validate._slice_native(native, page_fragment)

        self.assertIsNotNone(sliced)
        self.assertNotIn("Early value", sliced)
        self.assertEqual(sliced.count("Repeated category"), 1)
        self.assertIn("Omitted middle row", sliced)
        self.assertIn("Leading continuation text", sliced)
        self.assertLess(sliced.index("Later first"), sliced.index("Later fourth"))

    def test_native_slice_rejects_an_ambiguous_single_repeated_anchor(self):
        native = (
            "<table><tr><th>Label</th><th>Value</th></tr>"
            "<tr><td>Repeated category</td><td>Early</td></tr>"
            "<tr><td>Middle category</td><td>Middle</td></tr>"
            "<tr><td>Repeated category</td><td>Late</td></tr></table>"
        )
        fragment = (
            "<table><tr><th>Label</th><th>Value</th></tr>"
            "<tr><td>Repeated category</td><td>Observed</td></tr></table>"
        )

        self.assertIsNone(table_validate._slice_native(native, fragment))

    def test_native_match_uses_unique_ordered_text_for_split_cells(self):
        native = (
            "<table><tr><td>Review basis</td><td>Detailed explanation for the requested decision</td></tr>"
            "<tr><td>Final outcome</td><td>Approval remains subject to documented controls</td></tr></table>"
        )
        split = (
            "<table><tr><td>Review</td><td>basis</td><td>Detailed explanation</td></tr>"
            "<tr><td>for the requested decision</td><td>Final</td><td>outcome</td></tr>"
            "<tr><td>Approval remains</td><td>subject to documented</td><td>controls</td></tr></table>"
        )

        matched = table_validate.native_substitute(
            split, table_validate.prepare_native([{"html": native}])
        )

        self.assertEqual(matched, native)

    def test_ordered_native_match_rejects_ambiguous_equal_tables(self):
        first = (
            "<table><tr><td>Long review basis</td><td>Detailed explanation for the requested decision</td></tr>"
            "<tr><td>Final outcome</td><td>Approval remains subject to documented controls</td></tr></table>"
        )
        second = (
            "<table><tr><td>Long review basis Detailed explanation</td><td>for the requested decision</td></tr>"
            "<tr><td>Final outcome Approval remains</td><td>subject to documented controls</td></tr></table>"
        )
        split = (
            "<table><tr><td>Long</td><td>review basis</td><td>Detailed explanation</td></tr>"
            "<tr><td>for the requested decision</td><td>Final outcome</td><td>Approval remains</td></tr>"
            "<tr><td>subject</td><td>to documented</td><td>controls</td></tr></table>"
        )

        matched = table_validate.native_substitute(
            split,
            table_validate.prepare_native([{"html": first}, {"html": second}]),
        )

        self.assertIsNone(matched)

    def test_native_slice_aligns_a_keyless_boundary_row_to_stable_spans(self):
        native = (
            "<table><tr><th colspan='2'>Kind</th><th colspan='2'>Coverage</th><th>Notes</th></tr>"
            "<tr><td colspan='2'>First item</td><td colspan='2'>First coverage</td><td>One</td></tr>"
            "<tr><td colspan='2'>Second item</td><td colspan='2'>Second coverage</td><td>Two</td></tr>"
            "<tr><td colspan='2'>Third item</td><td colspan='2'>Third coverage</td><td>Three</td></tr>"
            "<tr><td colspan='2'>Fourth item</td><td colspan='2'>Fourth coverage</td><td>Four</td></tr></table>"
        )
        fragment = (
            "<table><tr><th colspan='2'>Kind</th><th colspan='2'>Coverage</th><th>Notes</th></tr>"
            "<tr><td></td><td>Leading continuation</td><td></td></tr>"
            "<tr><td>Second item</td><td>Second coverage</td><td>Two</td></tr>"
            "<tr><td>Third item</td><td>Third coverage</td><td>Three</td></tr></table>"
        )

        sliced = table_validate._slice_native(native, fragment)
        rows = table_validate._rows_of(
            BeautifulSoup(sliced, "html.parser").find("table")
        )
        _, widths, _, _ = table_validate._build_grid(rows)

        self.assertEqual(widths, [5, 5, 5, 5])
        self.assertIn("Leading continuation", sliced)

    def test_caption_duplicate_bracketed_metadata_row_is_removed(self):
        html = (
            "<table><tr><td colspan='3'>(Unit: millions)</td></tr>"
            "<tr><td>Item</td><td>Amount</td><td>Notes</td></tr>"
            "<tr><td>Alpha</td><td>10</td><td>Current</td></tr></table>"
        )

        cleaned = table_validate.strip_caption_duplicate_metadata_row(
            html, "Portfolio summary (Unit: millions)"
        )

        rows = table_validate._rows_of(
            BeautifulSoup(cleaned, "html.parser").find("table")
        )
        self.assertEqual(len(rows), 2)
        self.assertNotIn("Unit: millions", _visible_text(cleaned))

    def test_exact_caption_title_row_is_removed_before_real_headers(self):
        html = (
            "<table><tr><th colspan='2'>Sustainability goals</th></tr>"
            "<tr><th>Goal</th><th>Description</th></tr>"
            "<tr><td>Health</td><td>Healthy lives</td></tr></table>"
        )

        cleaned = table_validate.strip_caption_duplicate_metadata_row(
            html, "Sustainability goals"
        )

        rows = table_validate._rows_of(
            BeautifulSoup(cleaned, "html.parser").find("table")
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [
                cell.get_text(" ", strip=True)
                for cell in rows[0].find_all(["td", "th"], recursive=False)
            ],
            ["Goal", "Description"],
        )

    def test_adjacent_native_fragments_merge_without_losing_inner_details(self):
        native = (
            "<table><tr><th colspan='2'>Kind</th><th colspan='2'>Coverage</th><th>Notes</th></tr>"
            "<tr><td colspan='5'>Construction phase</td></tr>"
            "<tr><td colspan='2'>Primary policy</td><td colspan='2'>General terms</td><td>General note</td></tr>"
            "<tr><td colspan='2'>Delay policy</td><td colspan='2'>Delay terms</td><td>Delay note</td></tr>"
            "<tr><td colspan='2'>Liability policy</td><td colspan='2'>Liability terms</td><td>Liability note</td></tr></table>"
        )
        elements = [
            {
                "type": "table",
                "caption": "Coverage schedule",
                "content": (
                    "<table><tr><td>Kind</td><td>Coverage</td><td>Notes</td></tr>"
                    "<tr><td colspan='3'>Construction phase</td></tr></table>"
                ),
            },
            {
                "type": "table",
                "caption": "Coverage schedule",
                "content": (
                    "<table><tr><td rowspan='2'>Primary policy</td><td colspan='2'>General terms</td>"
                    "<td rowspan='2'>Reference codes</td></tr>"
                    "<tr><td>Part A</td><td>First detailed amount</td></tr>"
                    "<tr><td></td><td>Part B</td><td>Second detailed amount</td><td>General note</td></tr>"
                    "<tr><td>Delay policy</td><td colspan='2'>Delay terms</td><td>Delay note</td></tr>"
                    "<tr><td>Liability policy</td><td colspan='2'>Liability terms</td><td>Liability note</td></tr></table>"
                ),
            },
        ]

        merged = table_validate.merge_adjacent_native_table_fragments(
            elements, table_validate.prepare_native([{"html": native}])
        )

        self.assertEqual(len(merged), 1)
        self.assertIn("First detailed amount", merged[0]["content"])
        self.assertIn("Second detailed amount", merged[0]["content"])
        self.assertIn("General note", merged[0]["content"])
        quality = table_validate.assess_table_quality(
            merged[0]["content"], allow_nested=True
        )
        self.assertNotIn("ragged_rows", quality["issues"])
        self.assertEqual(quality["cols"], 5)

    def test_source_supported_missing_native_parent_wraps_unique_child(self):
        parent = (
            "<table>"
            "<tr><td>Outer category alpha</td><td>Outer category beta</td>"
            "<td>Outer category gamma</td><td>Outer category delta</td></tr>"
            "<tr><td>Review scope one</td><td>Review scope two</td>"
            "<td>Review scope three</td><td>Review scope four</td></tr>"
            "<tr><td>Detailed schedule</td><td colspan='3'></td></tr>"
            "</table>"
        )
        child = (
            "<table><tr><td>Item</td><td>Plan</td><td>Actual</td><td>Note</td></tr>"
            "<tr><td>North</td><td>10</td><td>9</td><td>Stable</td></tr>"
            "<tr><td>South</td><td>12</td><td>11</td><td>Stable</td></tr></table>"
        )
        elements = [{"type": "table", "content": child, "caption": "Schedule"}]
        source_text = " ".join(
            [
                "Outer category alpha",
                "Outer category beta",
                "Outer category gamma",
                "Outer category delta",
                "Review scope one",
                "Review scope two",
                "Review scope three",
                "Review scope four",
                "Detailed schedule",
                "Item Plan Actual Note North 10 9 Stable South 12 11 Stable",
            ]
        )

        restored = table_validate.restore_uniquely_supported_native_parents(
            elements,
            table_validate.prepare_native(
                [{"html": parent, "rows": 3, "cols": 4}, {"html": child, "rows": 3, "cols": 4}]
            ),
            source_text,
        )

        self.assertEqual(restored[0]["content"].count("<table"), 2)
        self.assertIn("Outer category alpha", restored[0]["content"])
        self.assertIn("North", restored[0]["content"])
        self.assertTrue(restored[0]["_native"])
        self.assertEqual(restored[0]["_source"], "native_nested_parent_restored")

    def test_missing_native_parent_requires_complete_page_text_support(self):
        parent = (
            "<table><tr><td>Alpha group label</td><td>Beta group label</td>"
            "<td>Gamma group label</td></tr>"
            "<tr><td>First scope label</td><td>Second scope label</td>"
            "<td>Third scope label</td></tr>"
            "<tr><td>Detail schedule label</td><td colspan='2'></td></tr></table>"
        )
        child = (
            "<table><tr><td>Item</td><td>Value</td></tr>"
            "<tr><td>North</td><td>10</td></tr></table>"
        )
        elements = [{"type": "table", "content": child}]
        incomplete_source = (
            "Alpha group label Beta group label Gamma group label "
            "First scope label Second scope label Detail schedule label "
            "Item Value North 10"
        )

        restored = table_validate.restore_uniquely_supported_native_parents(
            elements,
            table_validate.prepare_native([{"html": parent}, {"html": child}]),
            incomplete_source,
        )

        self.assertEqual(restored, elements)

    def test_long_native_table_is_sliced_to_unique_source_row_run(self):
        native = (
            "<table><tr><th>Category</th><th>Details</th></tr>"
            "<tr><td colspan='2'>Construction period</td></tr>"
            "<tr><td>Alpha coverage</td><td>Alpha details for current page</td></tr>"
            "<tr><td>Beta coverage</td><td>Beta details for current page</td></tr>"
            "<tr><td>Gamma coverage</td><td>Gamma details for current page</td></tr>"
            "<tr><td colspan='2'>Operation period</td></tr>"
            "<tr><td>Delta coverage</td><td>Delta details for later page</td></tr>"
            "<tr><td>Epsilon coverage</td><td>Epsilon details for later page</td></tr>"
            "<tr><td>Zeta coverage</td><td>Zeta details for later page</td></tr>"
            "<tr><td>Eta coverage</td><td>Eta details for later page</td></tr></table>"
        )
        source = (
            "Category Details Construction period Alpha coverage "
            "Alpha details for current page Beta coverage Beta details for current page "
            "Gamma coverage Gamma details for current page"
        )

        sliced = table_validate.slice_native_to_source_page(native, source)
        rows = table_validate._rows_of(
            BeautifulSoup(sliced, "html.parser").find("table")
        )

        self.assertEqual(len(rows), 5)
        self.assertIn("Gamma coverage", sliced)
        self.assertNotIn("Operation period", sliced)
        self.assertNotIn("Delta coverage", sliced)

    def test_native_page_slice_requires_multiple_supported_body_rows(self):
        native = (
            "<table><tr><th>Category</th><th>Details</th></tr>"
            + "".join(
                f"<tr><td>Coverage {index}</td><td>Unique later-page details {index}</td></tr>"
                for index in range(1, 9)
            )
            + "</table>"
        )
        source = (
            "Category Details Coverage 1 Unique later-page details 1 "
            "unrelated page prose that is deliberately long enough for review"
        )

        self.assertEqual(
            table_validate.slice_native_to_source_page(native, source), native
        )

    def test_formula_demote_is_blocked_when_a_grid_is_visible(self):
        original = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "content": (
                        "<table><tr><td>x<sub>i</sub>: item index</td><td>D<sub>i</sub>: item value</td></tr>"
                        "<tr><td>T</td><td>: total</td><td>t<sub>j</sub></td><td>: event date</td></tr>"
                        "<tr><td>P<sub>j</sub></td><td>: event amount</td></tr></table>"
                    ),
                }
            ],
        }
        image = Image.new("RGB", (600, 400), "white")
        draw = ImageDraw.Draw(image)
        for y in (50, 150, 250, 350):
            draw.line((50, y, 550, y), fill="black", width=3)

        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "grid.png"
            image.save(image_path)
            demoted, records = (
                table_quality_repair._demote_borderless_definition_tables(
                    image_path, original
                )
            )

        self.assertIsNone(demoted)
        self.assertEqual(records, [])

    def test_formula_demote_is_blocked_by_vertical_only_grid_borders(self):
        original = {
            "page_number": 1,
            "elements": [
                {
                    "type": "table",
                    "content": (
                        "<table><tr><td>x<sub>i</sub>: item index</td><td>D<sub>i</sub>: item value</td></tr>"
                        "<tr><td>T</td><td>: total</td><td>t<sub>j</sub></td><td>: event date</td></tr>"
                        "<tr><td>P<sub>j</sub></td><td>: event amount</td></tr></table>"
                    ),
                }
            ],
        }
        image = Image.new("RGB", (600, 400), "white")
        draw = ImageDraw.Draw(image)
        for x in (100, 300, 500):
            draw.line((x, 50, x, 350), fill="black", width=3)

        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "vertical-grid.png"
            image.save(image_path)
            demoted, records = (
                table_quality_repair._demote_borderless_definition_tables(
                    image_path, original
                )
            )

        self.assertIsNone(demoted)
        self.assertEqual(records, [])

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
