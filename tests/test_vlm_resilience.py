import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import utils


class VLMResilienceTests(unittest.TestCase):
    def test_flattened_page_is_split_around_exact_structured_table(self):
        intro = (
            "This overview preserves the narrative that appears before the data grid. "
            "It contains enough independent prose to prove that the surrounding text "
            "must not be discarded while duplicate structured content is removed. "
        )
        table = {
            "type": "table",
            "content": (
                "<table><tr><td>Metric</td><td>Value</td></tr>"
                "<tr><td>First category</td><td>10</td></tr>"
                "<tr><td>Second category</td><td>20</td></tr></table>"
            ),
        }
        trailing = {"type": "text", "content": "Approval follows after reviews."}
        flattened = {
            "type": "text",
            "content": (
                intro
                + "Metric Value First category 10 Second category 20 "
                + "Approval follows after review."
            ),
        }

        cleaned = utils._resolve_flattened_table_duplicates(
            [flattened, table, trailing]
        )

        self.assertEqual([item["type"] for item in cleaned], ["text", "table", "text"])
        self.assertEqual(cleaned[0]["content"].strip(), intro.strip())
        self.assertIs(cleaned[1], table)
        self.assertIs(cleaned[2], trailing)
        combined = " ".join(item.get("content", "") for item in cleaned)
        self.assertNotIn("Approval follows after review.", combined)
        self.assertEqual(combined.count("Approval follows after reviews."), 1)

    def test_flattened_page_rule_requires_the_complete_table(self):
        prose = (
            "A long narrative can mention First category without duplicating a whole "
            "table. " * 4
        )
        table = {
            "type": "table",
            "content": (
                "<table><tr><td>Metric</td><td>Value</td></tr>"
                "<tr><td>First category</td><td>10</td></tr>"
                "<tr><td>Second category</td><td>20</td></tr></table>"
            ),
        }
        elements = [{"type": "text", "content": prose}, table]

        self.assertEqual(utils._resolve_flattened_table_duplicates(elements), elements)

    def test_nearby_prose_repeated_inside_table_is_removed(self):
        table = {
            "type": "table",
            "content": (
                "<table><tr><td colspan='2'>(Unit: millions)</td></tr>"
                "<tr><td>Item</td><td>Amount</td></tr></table>"
            ),
        }
        elements = [
            table,
            {"type": "text", "content": "Supporting note"},
            {"type": "text", "content": "(Unit: millions)"},
        ]

        cleaned = utils._drop_prose_duplicated_by_nearby_tables(elements)

        self.assertEqual(cleaned, elements[:2])

    def test_text_preprocessing_preserves_links_and_email_addresses(self):
        source = "Contact qa@example.org or see https://example.org/guide?q=1."

        cleaned = utils.HWPTextExtractor.text_preprocessing(source)

        self.assertIn("qa@example.org", cleaned)
        self.assertIn("https://example.org/guide?q=1", cleaned)

    def test_dedupe_comparison_keeps_distinct_address_identity(self):
        first = utils._dedupe_comparison_text(
            "Contact <qa.one@example.org> at https://example.org/first"
        )
        second = utils._dedupe_comparison_text(
            "Contact <qa.two@example.org> at https://example.org/second"
        )

        self.assertNotEqual(first, second)
        self.assertIn("qaoneexampleorg", first)
        self.assertIn("exampleorgfirst", first)

    def test_dedupe_comparison_preserves_non_latin_scripts(self):
        normalized = utils._dedupe_comparison_text("東京 Отчёт تقرير")

        self.assertIn("東京", normalized)
        self.assertIn("отчёт", normalized)
        self.assertIn("تقرير", normalized)

    def test_trailing_duplicate_heading_cluster_is_removed(self):
        elements = [
            {"type": "heading_2", "content": "Section One"},
            {"type": "text", "content": "First body"},
            {"type": "heading_3", "content": "Topic A"},
            {"type": "text", "content": "Second body"},
            {"type": "heading_1", "content": "Section One"},
            {"type": "heading_2", "content": "Topic A"},
        ]

        cleaned = utils._drop_trailing_duplicate_heading_cluster(elements)

        self.assertEqual(cleaned, elements[:4])

    def test_unique_trailing_headings_are_preserved(self):
        elements = [
            {"type": "heading_1", "content": "Section One"},
            {"type": "text", "content": "Body"},
            {"type": "heading_2", "content": "Next Section"},
            {"type": "heading_3", "content": "Next Topic"},
        ]

        self.assertEqual(
            utils._drop_trailing_duplicate_heading_cluster(elements), elements
        )

    def test_trailing_duplicate_headings_before_a_year_are_removed(self):
        elements = [
            {"type": "heading_2", "content": "Section One"},
            {"type": "text", "content": "First body"},
            {"type": "heading_3", "content": "Topic A"},
            {"type": "text", "content": "Second body"},
            {"type": "heading_1", "content": "Section One"},
            {"type": "heading_2", "content": "Topic A"},
            {"type": "text", "content": "2024"},
        ]

        cleaned = utils._drop_trailing_duplicate_heading_cluster(elements)

        self.assertEqual(cleaned, elements[:4] + elements[-1:])

    def test_duplicate_figure_uses_text_layer_occurrence_count(self):
        figure = {
            "type": "figure",
            "content": (
                "<table><tr><td>Regional performance indicator</td>"
                "<td>Current reporting value</td></tr></table>"
            ),
            "caption": "Regional summary",
            "description": "A summary panel containing the regional values.",
        }
        elements = [figure, figure.copy()]
        text_layer = (
            "Regional summary Regional performance indicator "
            "Current reporting value"
        )

        cleaned = utils._dedupe_figures_supported_by_text_layer(
            elements, text_layer
        )

        self.assertEqual(cleaned, [figure])

    def test_duplicate_figure_is_kept_after_its_matching_heading(self):
        figure = {
            "type": "figure",
            "content": (
                "<table><tr><td>Regional performance indicator</td>"
                "<td>Current reporting value</td></tr></table>"
            ),
            "caption": "Regional summary procedure",
            "description": "A summary panel containing the regional values.",
        }
        heading = {"type": "heading_2", "content": "B. Regional summary"}
        text_layer = "B. Regional summary Regional performance indicator"

        cleaned = utils._dedupe_figures_supported_by_text_layer(
            [figure, heading, figure.copy()], text_layer
        )

        self.assertEqual(cleaned, [heading, figure])

    def test_visibly_repeated_figure_is_preserved_when_text_repeats(self):
        figure = {
            "type": "figure",
            "content": (
                "<table><tr><td>Regional performance indicator</td>"
                "<td>Current reporting value</td></tr></table>"
            ),
            "caption": "Regional summary",
            "description": "A summary panel containing the regional values.",
        }
        one_copy = (
            "Regional summary Regional performance indicator "
            "Current reporting value "
        )
        elements = [figure, figure.copy()]

        cleaned = utils._dedupe_figures_supported_by_text_layer(
            elements, one_copy + one_copy
        )

        self.assertEqual(cleaned, elements)

    def test_nonlocal_identical_figures_are_not_deduplicated(self):
        figure = {
            "type": "figure",
            "content": (
                "<table><tr><td>Regional performance indicator</td>"
                "<td>Current reporting value</td></tr></table>"
            ),
            "caption": "Regional summary",
            "description": "A summary panel containing the regional values.",
        }
        elements = [
            figure,
            {"type": "text", "content": "Independent discussion between figures."},
            figure.copy(),
        ]
        text_layer = "Regional summary Regional performance indicator"

        cleaned = utils._dedupe_figures_supported_by_text_layer(
            elements, text_layer
        )

        self.assertEqual(cleaned, elements)

    def test_figures_across_an_unrelated_heading_are_preserved(self):
        figure = {
            "type": "figure",
            "content": (
                "<table><tr><td>Regional performance indicator</td>"
                "<td>Current reporting value</td></tr></table>"
            ),
            "caption": "Regional summary",
            "description": "A summary panel containing the regional values.",
        }
        elements = [
            figure,
            {"type": "heading_2", "content": "Independent appendix"},
            figure.copy(),
        ]
        text_layer = "Regional summary Regional performance indicator"

        cleaned = utils._dedupe_figures_supported_by_text_layer(
            elements, text_layer
        )

        self.assertEqual(cleaned, elements)

    def test_duplicate_table_keeps_copy_with_matching_heading(self):
        table = {
            "type": "table",
            "caption": "Access preparation",
            "content": (
                "<table><tr><td>Item</td><td>Description</td></tr>"
                "<tr><td>Certificate</td><td>Prepare a valid access certificate before sign-in</td></tr>"
                "<tr><td>Storage</td><td>Keep the credential in an approved secure location</td></tr></table>"
            ),
        }
        elements = [
            table,
            {"type": "heading_2", "content": "3. Access preparation"},
            table.copy(),
        ]
        text_layer = (
            "3. Access preparation Item Description Certificate "
            "Prepare a valid access certificate before sign-in Storage "
            "Keep the credential in an approved secure location"
        )

        cleaned = utils._dedupe_tables_supported_by_text_layer(
            elements, text_layer
        )

        self.assertEqual(cleaned, elements[1:])

    def test_duplicate_heading_keeps_the_one_attached_to_its_content(self):
        elements = [
            {"type": "heading_2", "content": "3. Access preparation"},
            {"type": "heading_2", "content": "4. Registration"},
            {"type": "text", "content": "An unrelated explanatory paragraph with sufficient length."},
            {"type": "heading_2", "content": "3. Access preparation"},
            {
                "type": "table",
                "caption": "Access preparation",
                "content": "<table><tr><td>Item</td><td>Value</td></tr></table>",
            },
        ]

        cleaned = utils._dedupe_headings_supported_by_text_layer(
            elements, "3. Access preparation\n4. Registration\nItem Value"
        )

        self.assertEqual(
            [item.get("content") for item in cleaned if item["type"].startswith("heading_")],
            ["4. Registration", "3. Access preparation"],
        )

    def test_duplicate_heading_is_preserved_when_context_is_tied(self):
        elements = [
            {"type": "heading_2", "content": "Regional status"},
            {"type": "text", "content": "A first substantial body paragraph for this repeated card."},
            {"type": "heading_2", "content": "Regional status"},
            {"type": "text", "content": "A second substantial body paragraph for this repeated card."},
        ]

        cleaned = utils._dedupe_headings_supported_by_text_layer(
            elements, "Regional status\nA first substantial body paragraph"
        )

        self.assertEqual(cleaned, elements)

    def test_nearby_screenshot_text_is_kept_only_in_figure(self):
        text = {
            "type": "text",
            "content": "Select the confirmation control to complete account registration.",
        }
        figure = {
            "type": "figure",
            "content": (
                "Instruction: Select the confirmation control to complete account registration."
            ),
            "caption": "Registration screen",
            "description": "A screen showing the registration confirmation control.",
        }

        cleaned = utils._drop_prose_duplicated_by_nearby_figures(
            [text, {"type": "heading_2", "content": "Registration"}, figure],
            text["content"],
        )

        self.assertEqual(cleaned, [{"type": "heading_2", "content": "Registration"}, figure])

    def test_multiple_structured_instructions_are_not_folded_into_one_figure(self):
        first = {
            "type": "text",
            "content": "Choose the pending request from the filtered results list.",
        }
        second = {
            "type": "text",
            "content": "Confirm the selected request in the approval dialog.",
        }
        figure = {
            "type": "figure",
            "content": first["content"] + "\n" + second["content"],
            "caption": "Approval screen",
            "description": "A screen showing the request approval workflow.",
        }

        cleaned = utils._drop_prose_duplicated_by_nearby_figures(
            [first, second, figure], first["content"] + "\n" + second["content"]
        )

        self.assertEqual(cleaned, [first, second, figure])

    def test_matching_attached_page_counter_is_removed_from_edge_heading(self):
        elements = [
            {"type": "heading_1", "content": "Appendix transaction report - 1 8 -"},
            {"type": "text", "content": "Body content"},
        ]

        cleaned = utils._drop_page_artifact_elements(elements, page_no=18)

        self.assertEqual(cleaned[0]["content"], "Appendix transaction report")

    def test_nonmatching_numeric_title_suffix_is_preserved(self):
        elements = [
            {"type": "heading_1", "content": "Planning cycle - 2025 -"},
            {"type": "text", "content": "Body content"},
        ]

        cleaned = utils._drop_page_artifact_elements(elements, page_no=18)

        self.assertEqual(cleaned, elements)

    def test_excessive_stream_uses_semantic_source_ratio(self):
        with (
            patch.object(utils, "VLM_STREAM_ABORT_INPUT_MIN_CHARS", 10),
            patch.object(utils, "VLM_STREAM_ABORT_BASE_CHARS", 100),
            patch.object(utils, "VLM_STREAM_ABORT_INPUT_RATIO", 2.0),
        ):
            source = "A" * 60
            self.assertFalse(
                utils._stream_output_excessive("<td></td>" * 1000 + "B" * 100, source)
            )
            self.assertTrue(
                utils._stream_output_excessive("B" * 121, source)
            )
            self.assertFalse(utils._stream_output_excessive("B" * 1000, "short"))

    def test_metadata_retries_a_transient_failure(self):
        calls = []
        constructor_args = []

        class FakeCompletions:
            def create(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    raise TimeoutError("cold start")
                payload = {
                    "doc_title": "Generic report",
                    "date": None,
                    "organization": None,
                    "author": None,
                    "keywords": ["example"],
                }
                message = SimpleNamespace(content=json.dumps(payload))
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=message)]
                )

        class FakeOpenAI:
            def __init__(self, **kwargs):
                constructor_args.append(kwargs)
                self.chat = SimpleNamespace(completions=FakeCompletions())

        fake_openai = types.ModuleType("openai")
        fake_openai.OpenAI = FakeOpenAI
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict(sys.modules, {"openai": fake_openai}),
                patch.object(utils, "VLM_METADATA_ATTEMPTS", 2),
                patch.object(utils, "VLM_METADATA_TIMEOUT", 91),
                patch.object(utils.time, "sleep") as sleep,
            ):
                text_path = Path(tmp) / "page.txt"
                text_path.write_text("Generic report", encoding="utf-8")
                result = utils.VLMProcessor.extract_metadata(
                    [str(text_path)], [], "test-key", "test-model"
                )

        self.assertEqual(result["doc_title"], "Generic report")
        self.assertEqual(len(calls), 2)
        self.assertEqual(constructor_args[0]["timeout"], 91)
        self.assertTrue(all(call["timeout"] == 91 for call in calls))
        sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
