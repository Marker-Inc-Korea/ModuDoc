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
