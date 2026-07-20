import json
from pathlib import Path
import tempfile
import unittest

from tools.audit_validation_run import Audit


class AuditDuplicateTableTests(unittest.TestCase):
    def _audit_page(self, source_text):
        table = {
            "type": "table",
            "caption": "Processing status",
            "content": (
                "<table><tr><td>Processing category</td><td>Current result</td></tr>"
                "<tr><td>Identity verification</td><td>No matching records were returned</td></tr>"
                "</table>"
            ),
        }
        elements = [
            {"type": "heading_3", "content": "4.1 First check"},
            table,
            {"type": "heading_3", "content": "4.2 Second check"},
            table.copy(),
        ]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        doc_dir = Path(temporary.name) / "document"
        doc_dir.mkdir()
        structured = doc_dir / "page_0001_structured.json"
        structured.write_text(
            json.dumps({"page_number": 1, "elements": elements}),
            encoding="utf-8",
        )
        structured.with_name("page_0001.txt").write_text(
            source_text, encoding="utf-8"
        )
        audit = Audit(Path(temporary.name))
        audit.audit_page(doc_dir, structured)
        return audit

    def test_distinct_section_tables_are_not_hard_failures_without_text_layer(self):
        audit = self._audit_page("Sparse page text")

        self.assertFalse(
            any(
                item["issue"] == "duplicate_substantial_element"
                for item in audit.hard_failures
            )
        )
        self.assertTrue(
            any(
                item["issue"] == "repeated_table_without_text_layer_evidence"
                for item in audit.warnings
            )
        )

    def test_source_supported_single_copy_remains_a_hard_duplicate(self):
        audit = self._audit_page(
            "Processing category Current result Identity verification "
            "No matching records were returned"
        )

        self.assertTrue(
            any(
                item["issue"] == "duplicate_substantial_element"
                for item in audit.hard_failures
            )
        )

    def test_source_supported_repetition_is_not_a_duplicate(self):
        one_copy = (
            "Processing category Current result Identity verification "
            "No matching records were returned "
        )
        audit = self._audit_page(one_copy * 2)

        self.assertFalse(
            any(
                item["issue"] == "duplicate_substantial_element"
                for item in audit.hard_failures
            )
        )


if __name__ == "__main__":
    unittest.main()
