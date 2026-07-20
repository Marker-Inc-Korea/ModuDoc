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
