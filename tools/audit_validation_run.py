#!/usr/bin/env python3
"""Run deterministic completeness and structure checks on a parser run."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import table_validate  # noqa: E402
from utils import (  # noqa: E402
    _drop_page_artifact_elements,
    _table_text_layer_copy_support,
)


HARD_TABLE_ISSUES = {
    "no_table",
    "empty_table",
    "ragged_rows",
    "nested_table",
    "quality_check_error",
}

PARSER_HARD_TABLE_ISSUES = {
    "validator_error",
    "quality_check_error",
    "nested_table",
}


def page_number(path: Path) -> int | None:
    match = re.search(r"page_(\d+)", path.name)
    return int(match.group(1)) if match else None


def normalized_content(element: dict) -> str:
    text = " ".join(
        str(element.get(key) or "") for key in ("content", "caption", "description")
    )
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def nonempty_json(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if isinstance(data, (list, dict)):
        return bool(data)
    return False


def distinct_table_contexts(elements: list[dict], indices: list[int]) -> bool:
    """Check whether repeated tables belong to distinct nearby sections."""
    contexts = []
    for position, index in enumerate(indices):
        lower = indices[position - 1] + 1 if position else max(0, index - 4)
        heading = ""
        for candidate_index in range(index - 1, lower - 1, -1):
            candidate = elements[candidate_index]
            if str(candidate.get("type") or "").startswith("heading_"):
                heading = normalized_content(candidate)
                break
        if not heading:
            return False
        contexts.append(heading)
    return len(set(contexts)) == len(contexts)


class Audit:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.hard_failures: list[dict] = []
        self.warnings: list[dict] = []
        self.stats = Counter()

    def hard(self, where: str, issue: str, detail: object = None) -> None:
        item = {"where": where, "issue": issue}
        if detail is not None:
            item["detail"] = detail
        self.hard_failures.append(item)

    def warn(self, where: str, issue: str, detail: object = None) -> None:
        item = {"where": where, "issue": issue}
        if detail is not None:
            item["detail"] = detail
        self.warnings.append(item)

    def audit_page(self, doc_dir: Path, structured_path: Path) -> None:
        page = page_number(structured_path)
        where = f"{doc_dir.name}/page_{page if page is not None else '?'}"
        try:
            data = json.loads(structured_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.hard(where, "invalid_structured_json", str(exc))
            return
        if not isinstance(data, dict):
            self.hard(where, "structured_json_not_object")
            return
        elements = data.get("elements")
        if not isinstance(elements, list):
            self.hard(where, "elements_not_list")
            return
        elements = [item for item in elements if isinstance(item, dict)]
        self.stats["elements"] += len(elements)
        if not elements:
            self.hard(where, "empty_elements")

        residual = _drop_page_artifact_elements(elements, page)
        if len(residual) != len(elements):
            self.hard(where, "residual_page_artifact", len(elements) - len(residual))

        substantial_groups: dict[str, list[int]] = {}
        for index, element in enumerate(elements):
            content = normalized_content(element)
            if len(content) >= 40:
                substantial_groups.setdefault(content, []).append(index)

            element_type = str(element.get("type") or "")
            try:
                confidence = float(element.get("_confidence", 1.0))
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < 0.75 and element_type != "table":
                self.warn(where, "low_confidence_element", {"index": index, "type": element_type, "confidence": confidence})

            if element_type == "figure" and not str(element.get("description") or "").strip():
                self.hard(where, "figure_missing_description", index)
            if element_type != "table":
                continue

            self.stats["tables"] += 1
            html = str(element.get("content") or "")
            if not html.strip():
                self.hard(where, "table_missing_content", index)
                continue
            allow_nested = element.get("_source") == "native_table"
            parser_issues = {
                str(issue) for issue in (element.get("_issues") or []) if isinstance(issue, str)
            }
            if allow_nested:
                parser_issues.discard("nested_table")
            hard_parser_issues = sorted(parser_issues & PARSER_HARD_TABLE_ISSUES)
            if hard_parser_issues:
                self.hard(
                    where,
                    "parser_table_issue",
                    {"index": index, "issues": hard_parser_issues},
                )
            quality = table_validate.assess_table_quality(
                html, element.get("caption"), allow_nested=allow_nested
            )
            issues = set(str(issue) for issue in quality.get("issues") or [])
            hard_issues = sorted(issues & HARD_TABLE_ISSUES)
            if hard_issues:
                self.hard(where, "table_quality_issue", {"index": index, "issues": hard_issues, "quality": quality})
            try:
                table_confidence = float(quality.get("confidence", 0.0))
            except (TypeError, ValueError):
                table_confidence = 0.0
            if table_confidence < 0.75:
                self.hard(where, "table_confidence_below_0.75", {"index": index, "quality": quality})
            if not str(element.get("caption") or "").strip():
                self.warn(where, "table_missing_caption", index)

        text_path = structured_path.with_name(
            structured_path.name.replace("_structured.json", ".txt")
        )
        try:
            source_text = text_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            source_text = ""
        for content, indices in substantial_groups.items():
            if len(indices) < 2:
                continue
            repeated = [elements[index] for index in indices]
            if all(element.get("type") == "table" for element in repeated):
                support = _table_text_layer_copy_support(repeated[0], source_text)
                if support is not None and support >= len(indices):
                    continue
                if support is None and distinct_table_contexts(elements, indices):
                    self.warn(
                        where,
                        "repeated_table_without_text_layer_evidence",
                        {"indices": indices},
                    )
                    continue
            for duplicate in indices[1:]:
                self.hard(
                    where,
                    "duplicate_substantial_element",
                    {"first": indices[0], "duplicate": duplicate, "text": content[:160]},
                )

    def audit_document(self, doc_dir: Path) -> None:
        metadata_path = doc_dir / "metadata.json"
        where = doc_dir.name
        if not metadata_path.exists():
            self.hard(where, "metadata_missing")
        else:
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception as exc:
                self.hard(where, "metadata_invalid", str(exc))
                metadata = {}
            failed_pages = metadata.get("vlm_failed_pages") or []
            low_pages = metadata.get("low_confidence_pages") or []
            if failed_pages:
                self.hard(where, "vlm_failed_pages", failed_pages)
            if low_pages:
                self.warn(where, "low_confidence_pages", low_pages)

        images = {page_number(path): path for path in doc_dir.glob("page_*.png")}
        structured = {page_number(path): path for path in doc_dir.glob("page_*_structured.json")}
        self.stats["images"] += len(images)
        self.stats["structured_pages"] += len(structured)
        for missing in sorted(set(images) - set(structured), key=lambda item: (-1 if item is None else item)):
            self.hard(where, "image_without_structured_page", missing)
        for missing in sorted(set(structured) - set(images), key=lambda item: (-1 if item is None else item)):
            self.hard(where, "structured_page_without_image", missing)
        if not images:
            self.hard(where, "no_page_images")

        for path in structured.values():
            self.audit_page(doc_dir, path)

        for chunk_name in ("split_page.json", "split_toc.json", "split_tree.json"):
            chunk_path = doc_dir / chunk_name
            if not chunk_path.exists():
                self.hard(where, "chunk_output_missing", chunk_name)
            elif not nonempty_json(chunk_path):
                self.hard(where, "chunk_output_empty_or_invalid", chunk_name)

    def report(self, expected_documents: int | None, expected_pages: int | None) -> dict:
        docs = sorted(
            path for path in self.run_dir.iterdir() if path.is_dir() and not path.name.startswith("_")
        )
        self.stats["documents"] = len(docs)
        if expected_documents is not None and len(docs) != expected_documents:
            self.hard("run", "unexpected_document_count", {"expected": expected_documents, "actual": len(docs)})
        for doc_dir in docs:
            self.audit_document(doc_dir)
        if expected_pages is not None and self.stats["images"] != expected_pages:
            self.hard("run", "unexpected_page_count", {"expected": expected_pages, "actual": self.stats["images"]})
        if self.stats["images"] != self.stats["structured_pages"]:
            self.hard(
                "run",
                "page_alignment_count_mismatch",
                {"images": self.stats["images"], "structured": self.stats["structured_pages"]},
            )
        return {
            "run_dir": str(self.run_dir),
            "pass": not self.hard_failures,
            "stats": dict(self.stats),
            "hard_failure_count": len(self.hard_failures),
            "warning_count": len(self.warnings),
            "hard_failures": self.hard_failures,
            "warnings": self.warnings,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir")
    parser.add_argument("--expected-documents", type=int)
    parser.add_argument("--expected-pages", type=int)
    parser.add_argument("--out")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_dir = Path(args.run_dir).expanduser().resolve()
    if not run_dir.is_dir():
        print(f"run directory not found: {run_dir}", file=sys.stderr)
        return 2
    report = Audit(run_dir).report(args.expected_documents, args.expected_pages)
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        out = Path(args.out).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(output, encoding="utf-8")
    print(
        f"deterministic_audit pass={report['pass']} documents={report['stats'].get('documents', 0)} "
        f"pages={report['stats'].get('images', 0)} tables={report['stats'].get('tables', 0)} "
        f"hard={report['hard_failure_count']} warnings={report['warning_count']}",
        flush=True,
    )
    for failure in report["hard_failures"][:50]:
        print(f"HARD {failure['where']}: {failure['issue']} {failure.get('detail', '')}", flush=True)
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
