#!/usr/bin/env python3
"""Check parser output directories against lightweight golden rules.

This intentionally runs without a GPU. It validates deterministic invariants
on *_structured.json files and reuses table_validate quality signals for table
rules.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

try:
    import table_validate
except Exception:  # pragma: no cover - verifier still works without table checks.
    table_validate = None


TEMPLATE = {
    "documents": {
        "regex:sample-document-name": {
            "pages": {
                "1": {
                    "required_text": ["expected visible text"],
                    "forbidden_text": ["known hallucination"],
                    "min_elements": 1,
                    "allow_low_confidence": False,
                    "required_element_types": {"table": 1},
                    "tables": [
                        {
                            "caption_contains": "table title",
                            "min_rows": 2,
                            "min_cols": 2,
                            "forbidden_issues": ["ragged_rows"],
                            "min_confidence": 0.75,
                        }
                    ],
                }
            }
        }
    }
}


def squash(text: object) -> str:
    text = "" if text is None else str(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def contains_text(haystack: str, needle: str) -> bool:
    return squash(needle) in squash(haystack)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def page_path(doc_dir: Path, page: str) -> Path | None:
    try:
        page_num = int(page)
    except ValueError:
        page_num = None
    candidates = []
    if page_num is not None:
        candidates.append(doc_dir / f"page_{page_num:04d}_structured.json")
    candidates.append(doc_dir / f"page_{page}_structured.json")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    if page_num is not None:
        hits = sorted(doc_dir.glob(f"page_*{page_num}*_structured.json"))
        if hits:
            return hits[0]
    return None


def structured_text(data: dict) -> str:
    parts = []
    for elem in data.get("elements") or []:
        if not isinstance(elem, dict):
            continue
        for key in ("content", "caption", "description"):
            if elem.get(key):
                parts.append(squash(elem.get(key)))
    return "\n".join(parts)


def element_types(elements: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for elem in elements:
        etype = str(elem.get("type") or "unknown")
        counts[etype] = counts.get(etype, 0) + 1
    return counts


def element_sources(elements: list[dict]) -> set[str]:
    return {str(elem.get("_source")) for elem in elements if elem.get("_source")}


def element_issues(elements: list[dict]) -> list[str]:
    issues = []
    for elem in elements:
        if not isinstance(elem.get("_issues"), list):
            continue
        issues.extend(str(issue) for issue in elem["_issues"])
    return issues


def low_confidence_elements(elements: list[dict]) -> int:
    count = 0
    for elem in elements:
        try:
            if float(elem.get("_confidence", 1.0)) < 0.75:
                count += 1
        except (TypeError, ValueError):
            continue
    return count


def match_documents(run_dir: Path, rule_key: str) -> list[Path]:
    docs = sorted(path for path in run_dir.iterdir() if path.is_dir() and not path.name.startswith("_"))
    if rule_key.startswith("regex:"):
        rx = re.compile(rule_key[len("regex:") :])
        return [path for path in docs if rx.search(path.name)]
    exact = run_dir / rule_key
    return [exact] if exact.is_dir() else []


class Recorder:
    def __init__(self) -> None:
        self.checks = 0
        self.failures: list[dict] = []

    def check(self, ok: bool, where: str, message: str, detail: object = None) -> None:
        self.checks += 1
        if ok:
            return
        failure = {"where": where, "message": message}
        if detail is not None:
            failure["detail"] = detail
        self.failures.append(failure)


def find_table_candidates(elements: list[dict], rule: dict) -> list[dict]:
    candidates = [elem for elem in elements if elem.get("type") == "table" and elem.get("content")]
    caption_need = rule.get("caption_contains")
    content_need = rule.get("content_contains")
    if caption_need:
        candidates = [elem for elem in candidates if contains_text(elem.get("caption", ""), caption_need)]
    if content_need:
        candidates = [elem for elem in candidates if contains_text(elem.get("content", ""), content_need)]
    return candidates


def assess_table(elem: dict) -> dict:
    issues = list(elem.get("_issues") or [])
    out = {"confidence": elem.get("_confidence", 0.85), "issues": issues, "rows": None, "cols": None}
    if table_validate is None:
        return out
    quality = table_validate.assess_table_quality(elem.get("content"), elem.get("caption"))
    merged = list(dict.fromkeys(issues + list(quality.get("issues") or [])))
    return {
        "confidence": quality.get("confidence", out["confidence"]),
        "issues": merged,
        "rows": quality.get("rows"),
        "cols": quality.get("cols"),
    }


def verify_page(rec: Recorder, where: str, page_file: Path, rule: dict) -> None:
    data = load_json(page_file)
    elements = [elem for elem in data.get("elements") or [] if isinstance(elem, dict)]
    text = structured_text(data)
    counts = element_types(elements)
    issues = element_issues(elements)
    sources = element_sources(elements)

    if "min_elements" in rule:
        rec.check(len(elements) >= int(rule["min_elements"]), where, "too few elements", len(elements))
    if "max_elements" in rule:
        rec.check(len(elements) <= int(rule["max_elements"]), where, "too many elements", len(elements))

    if rule.get("allow_low_confidence") is False:
        low_count = low_confidence_elements(elements)
        rec.check(not data.get("low_confidence"), where, "page marked low_confidence")
        rec.check(low_count == 0, where, "low confidence elements present", low_count)
    if "max_low_confidence_elements" in rule:
        low_count = low_confidence_elements(elements)
        rec.check(low_count <= int(rule["max_low_confidence_elements"]), where, "too many low confidence elements", low_count)

    for snippet in rule.get("required_text") or []:
        rec.check(contains_text(text, snippet), where, "required text missing", snippet)
    for snippet in rule.get("forbidden_text") or []:
        rec.check(not contains_text(text, snippet), where, "forbidden text present", snippet)
    for pattern in rule.get("required_regex") or []:
        rec.check(bool(re.search(pattern, text, re.MULTILINE)), where, "required regex missing", pattern)
    for pattern in rule.get("forbidden_regex") or []:
        rec.check(not re.search(pattern, text, re.MULTILINE), where, "forbidden regex present", pattern)

    for etype, min_count in (rule.get("required_element_types") or {}).items():
        rec.check(counts.get(etype, 0) >= int(min_count), where, "required element type missing", {etype: counts.get(etype, 0)})
    for source in rule.get("required_sources") or []:
        rec.check(source in sources, where, "required source missing", source)
    for issue in rule.get("required_issues") or []:
        rec.check(issue in issues, where, "required issue missing", issue)
    for issue in rule.get("forbidden_issues") or []:
        rec.check(issue not in issues, where, "forbidden issue present", issue)

    for idx, table_rule in enumerate(rule.get("tables") or [], 1):
        table_where = f"{where}/table[{idx}]"
        candidates = find_table_candidates(elements, table_rule)
        rec.check(bool(candidates), table_where, "matching table not found", table_rule)
        if not candidates:
            continue
        if "index" in table_rule:
            table_index = int(table_rule["index"])
            elem = candidates[table_index] if table_index < len(candidates) else None
            rec.check(elem is not None, table_where, "table index out of range", table_index)
            if elem is None:
                continue
        else:
            elem = candidates[0]
        quality = assess_table(elem)
        if "min_rows" in table_rule:
            rec.check((quality.get("rows") or 0) >= int(table_rule["min_rows"]), table_where, "too few table rows", quality)
        if "min_cols" in table_rule:
            rec.check((quality.get("cols") or 0) >= int(table_rule["min_cols"]), table_where, "too few table columns", quality)
        if "min_confidence" in table_rule:
            rec.check(float(quality.get("confidence") or 0) >= float(table_rule["min_confidence"]), table_where, "table confidence too low", quality)
        for issue in table_rule.get("required_issues") or []:
            rec.check(issue in (quality.get("issues") or []), table_where, "required table issue missing", quality)
        for issue in table_rule.get("forbidden_issues") or []:
            rec.check(issue not in (quality.get("issues") or []), table_where, "forbidden table issue present", quality)


def verify(run_dir: Path, rules: dict) -> dict:
    rec = Recorder()
    for doc_rule_key, doc_rule in (rules.get("documents") or {}).items():
        matched = match_documents(run_dir, doc_rule_key)
        rec.check(bool(matched), doc_rule_key, "document rule matched no output directory")
        for doc_dir in matched:
            for page, page_rule in (doc_rule.get("pages") or {}).items():
                where = f"{doc_dir.name}/page_{page}"
                pfile = page_path(doc_dir, str(page))
                rec.check(pfile is not None, where, "structured page json missing")
                if pfile is None:
                    continue
                verify_page(rec, where, pfile, page_rule)
    return {
        "run_dir": str(run_dir),
        "checks": rec.checks,
        "failures": rec.failures,
        "pass": not rec.failures,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", nargs="?", help="Parser output directory containing document subdirectories")
    parser.add_argument("--rules", help="Path to golden rules JSON")
    parser.add_argument("--rules-json", help="Rules JSON string; useful for CI one-liners")
    parser.add_argument("--out", help="Write summary JSON to this path")
    parser.add_argument("--write-template", help="Write a starter rules file and exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.write_template:
        Path(args.write_template).write_text(json.dumps(TEMPLATE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    if not args.run_dir:
        raise SystemExit("run_dir is required unless --write-template is used")
    if args.rules_json:
        rules = json.loads(args.rules_json)
    elif args.rules:
        rules = load_json(Path(args.rules))
    else:
        raise SystemExit("provide --rules or --rules-json")

    summary = verify(Path(args.run_dir), rules)
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
