"""Targeted VLM repair for pages with objectively low-quality table structure."""

from __future__ import annotations

import base64
import argparse
import io
import json
import logging
import os
import re
from pathlib import Path

from bs4 import BeautifulSoup

import table_validate


logger = logging.getLogger(__name__)

TABLE_QUALITY_REPAIR = os.environ.get("TABLE_QUALITY_REPAIR", "1") == "1"
TABLE_QUALITY_REPAIR_MAX_PAGES = max(0, int(os.environ.get("TABLE_QUALITY_REPAIR_MAX_PAGES", "8")))
TABLE_QUALITY_REPAIR_MAX_TOKENS = max(1024, int(os.environ.get("TABLE_QUALITY_REPAIR_MAX_TOKENS", "16384")))
TABLE_QUALITY_REPAIR_TIMEOUT = max(60, int(os.environ.get("TABLE_QUALITY_REPAIR_TIMEOUT", "600")))
TABLE_QUALITY_REPAIR_IMG_MAXW = max(1024, int(os.environ.get("TABLE_QUALITY_REPAIR_IMG_MAXW", "2464")))
TABLE_QUALITY_REPAIR_MIN_COVERAGE = min(
    1.0, max(0.5, float(os.environ.get("TABLE_QUALITY_REPAIR_MIN_COVERAGE", "0.75")))
)

HARD_ISSUES = {
    "no_table",
    "empty_table",
    "ragged_rows",
    "nested_table",
    "quality_check_error",
}

SYSTEM_PROMPT = """You are repairing a document page whose previous table extraction failed structural QA.
Use the PAGE IMAGE as the sole source of truth. Return the COMPLETE page, not only the broken table.
Output ONLY one valid JSON object:
{"page_number": int, "elements": [{"type": "heading_1|heading_2|heading_3|toc_entry|text|table|figure|footnote", "content": "...", "caption": "", "description": ""}]}

Rules:
- Preserve every visible heading, body block, key-value banner, table, note, and figure in reading order.
- Transcribe only visible content. Do not infer, summarize, translate, or silently correct wording.
- A table is HTML using <table>, <tr>, <td>, <br>, colspan, and rowspan. Do not use <th>.
- One continuous bordered grid is one table. Side-by-side grids are separate tables. Top/bottom grids with a separate border or gap and different column structures are separate tables.
- Preserve two visually repeated tables even when their cells contain identical text.
- Keep a genuinely nested grid inside its parent <td> only when it is visibly inside that outer cell.
- Every table must expand to a rectangle after colspan/rowspan. Do not add an all-empty padding column.
- Preserve every visually merged header and body cell with the corresponding
  colspan or rowspan. Never shift values between columns or omit trailing cells.
- Return valid JSON without markdown fences or commentary."""


def _norm(text: object) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", str(text or "")).casefold()


def _page_number(path: Path) -> int:
    match = re.search(r"page_(\d+)", path.name)
    return int(match.group(1)) if match else 0


def _public_candidate(data: dict) -> str:
    clean = {
        "page_number": data.get("page_number"),
        "elements": [],
    }
    for element in data.get("elements") or []:
        if not isinstance(element, dict):
            continue
        clean["elements"].append(
            {
                key: element.get(key)
                for key in ("type", "content", "caption", "description")
                if element.get(key) not in (None, "")
            }
        )
    return json.dumps(clean, ensure_ascii=False)


def _problem_tables(data: dict) -> list[dict]:
    problems = []
    for index, element in enumerate(data.get("elements") or []):
        if not isinstance(element, dict) or element.get("type") != "table":
            continue
        allow_nested = element.get("_source") == "native_table"
        quality = table_validate.assess_table_quality(
            element.get("content"), element.get("caption"), allow_nested=allow_nested
        )
        issues = set(quality.get("issues") or []) & HARD_ISSUES
        if issues or float(quality.get("confidence") or 0.0) < 0.75:
            problems.append({"index": index, "issues": sorted(issues), "quality": quality})
    return problems


def _preview_tables(data: dict) -> dict:
    preview = {**data, "elements": []}
    for element in data.get("elements") or []:
        if not isinstance(element, dict) or element.get("type") != "table" or not element.get("content"):
            preview["elements"].append(element)
            continue
        repaired, _, _ = table_validate.validate_and_repair_table(
            element.get("content"), element.get("caption")
        )
        for item in repaired:
            candidate = {**element, "content": item.get("content")}
            if item.get("caption") is not None:
                candidate["caption"] = item.get("caption")
            preview["elements"].append(candidate)
    return preview


def _content_chunks(data: dict) -> set[str]:
    chunks = set()
    for element in data.get("elements") or []:
        if not isinstance(element, dict):
            continue
        if element.get("type") == "table":
            soup = BeautifulSoup(element.get("content") or "", "html.parser")
            values = [cell.get_text(" ", strip=True) for cell in soup.find_all(["td", "th"])]
            if element.get("caption"):
                values.append(str(element.get("caption")))
        else:
            values = re.split(r"[\n.!?。]+", str(element.get("content") or ""))
            values += [str(element.get(key) or "") for key in ("caption", "description")]
        for value in values:
            normalized = _norm(value)
            if len(normalized) >= 2:
                chunks.add(normalized)
    return chunks


def _orphan_same_level_headings(data: dict) -> int:
    elements = [item for item in (data.get("elements") or []) if isinstance(item, dict)]
    count = 0
    for index, element in enumerate(elements[:-1]):
        element_type = str(element.get("type") or "")
        if not re.fullmatch(r"heading_[123]", element_type):
            continue
        if str(elements[index + 1].get("type") or "") == element_type:
            count += 1
    return count


def candidate_improvement(original: dict, candidate: dict) -> tuple[bool, dict]:
    """Require structural improvement without dropping existing page content."""
    candidate = _preview_tables(candidate)
    old_problems = _problem_tables(original)
    new_problems = _problem_tables(candidate)
    old_tables = sum(1 for item in original.get("elements") or [] if isinstance(item, dict) and item.get("type") == "table")
    new_tables = sum(1 for item in candidate.get("elements") or [] if isinstance(item, dict) and item.get("type") == "table")
    old_chunks = _content_chunks(original)
    new_chunks = _content_chunks(candidate)
    coverage = sum(1 for chunk in old_chunks if chunk in new_chunks) / max(1, len(old_chunks))
    old_chars = len(_norm(_public_candidate(original)))
    new_chars = len(_norm(_public_candidate(candidate)))
    char_ratio = new_chars / max(1, old_chars)
    old_orphans = _orphan_same_level_headings(original)
    new_orphans = _orphan_same_level_headings(candidate)
    metrics = {
        "old_problem_tables": len(old_problems),
        "new_problem_tables": len(new_problems),
        "old_tables": old_tables,
        "new_tables": new_tables,
        "coverage": round(coverage, 4),
        "char_ratio": round(char_ratio, 4),
        "old_orphan_headings": old_orphans,
        "new_orphan_headings": new_orphans,
    }
    accepted = bool(
        old_problems
        and len(new_problems) < len(old_problems)
        and new_tables >= old_tables
        and coverage >= TABLE_QUALITY_REPAIR_MIN_COVERAGE
        and char_ratio >= TABLE_QUALITY_REPAIR_MIN_COVERAGE
        and (old_orphans == 0 or new_orphans < old_orphans)
    )
    return accepted, metrics


def _encode_image(path: Path) -> str:
    from PIL import Image

    with Image.open(path) as image:
        image = image.convert("RGB")
        if image.width > TABLE_QUALITY_REPAIR_IMG_MAXW:
            ratio = TABLE_QUALITY_REPAIR_IMG_MAXW / image.width
            image = image.resize(
                (TABLE_QUALITY_REPAIR_IMG_MAXW, int(image.height * ratio)), Image.LANCZOS
            )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _first_json(text: str) -> dict | None:
    raw = (text or "").strip()
    if raw.startswith("```json"):
        raw = raw[7:]
    elif raw.startswith("```"):
        raw = raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    raw = raw.strip()
    try:
        value, _ = json.JSONDecoder().raw_decode(raw)
    except Exception:
        return None
    if isinstance(value, list):
        value = {"page_number": 0, "elements": value}
    return value if isinstance(value, dict) else None


def _request_candidate(client, model: str, image_path: Path, text_path: Path, original: dict, problems: list[dict]) -> dict | None:
    raw_text = text_path.read_text(encoding="utf-8", errors="replace") if text_path.exists() else ""
    user_text = (
        f"Detected structural problems: {json.dumps(problems, ensure_ascii=False)}\n\n"
        "Previous candidate (use only to avoid dropping correct content; image wins on every conflict):\n"
        f"<candidate>{_public_candidate(original)}</candidate>\n\n"
        f"Raw text layer (may be noisy):\n<raw_text>{raw_text[:20000]}</raw_text>\n\n"
        "Re-extract and repair the complete page."
    )
    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_encode_image(image_path)}"}},
                    {"type": "text", "text": user_text},
                ],
            },
        ],
        "temperature": 0,
        "max_tokens": TABLE_QUALITY_REPAIR_MAX_TOKENS,
        "timeout": TABLE_QUALITY_REPAIR_TIMEOUT,
        "extra_body": {"repetition_penalty": 1.03},
    }
    try:
        response = client.chat.completions.create(
            **request, response_format={"type": "json_object"}
        )
    except Exception:
        response = client.chat.completions.create(**request)
    return _first_json(response.choices[0].message.content or "")


def repair_low_quality_pages(doc_output_dir: str, api_key: str, model: str, persist_page) -> list[dict]:
    """Repair objectively broken table pages and persist only strict improvements."""
    if not TABLE_QUALITY_REPAIR:
        return []
    from openai import OpenAI

    root = Path(doc_output_dir)
    candidates = []
    for structured_path in sorted(root.glob("page_*_structured.json")):
        try:
            data = json.loads(structured_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        problems = _problem_tables(data)
        if problems:
            candidates.append((structured_path, data, problems))
    if TABLE_QUALITY_REPAIR_MAX_PAGES > 0:
        candidates = candidates[:TABLE_QUALITY_REPAIR_MAX_PAGES]
    if not candidates:
        return []

    client = OpenAI(
        base_url=os.environ.get("VLM_BASE_URL", "http://localhost:8000/v1"),
        api_key=api_key or "EMPTY",
        timeout=TABLE_QUALITY_REPAIR_TIMEOUT,
        max_retries=0,
    )
    results = []
    for structured_path, original, problems in candidates:
        stem = structured_path.name[: -len("_structured.json")]
        image_path = root / f"{stem}.png"
        text_path = root / f"{stem}.txt"
        record = {"page": _page_number(structured_path), "accepted": False, "problems": problems}
        if not image_path.exists():
            record["error"] = "page image missing"
            results.append(record)
            continue
        try:
            candidate = _request_candidate(client, model, image_path, text_path, original, problems)
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            results.append(record)
            continue
        if not candidate or not isinstance(candidate.get("elements"), list) or not candidate.get("elements"):
            record["error"] = "repair returned invalid or empty JSON"
            results.append(record)
            continue
        candidate["page_number"] = record["page"]
        candidate["quality_repair"] = True
        candidate = _preview_tables(candidate)
        accepted, metrics = candidate_improvement(original, candidate)
        record["metrics"] = metrics
        if not accepted:
            record["error"] = "candidate did not meet lossless improvement gates"
            results.append(record)
            continue
        persist_page(stem, json.dumps(candidate, ensure_ascii=False))
        record["accepted"] = True
        results.append(record)
        logger.info("%s targeted table-quality repair accepted: %s", stem, metrics)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir")
    parser.add_argument("--model", default="Qwen/Qwen3-VL-30B-A3B-Instruct")
    parser.add_argument("--out")
    args = parser.parse_args()
    root = Path(args.run_dir).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"run directory not found: {root}")

    report = {"run_dir": str(root), "documents": {}, "accepted": 0, "rejected": 0}
    for doc_dir in sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith("_")):
        def persist(stem, structured_data, directory=doc_dir):
            parsed = json.loads(structured_data)
            (directory / f"{stem}_structured.json").write_text(
                json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )

        results = repair_low_quality_pages(
            str(doc_dir), os.environ.get("VLM_API_KEY", "EMPTY"), args.model, persist
        )
        report["documents"][doc_dir.name] = results
        report["accepted"] += sum(1 for item in results if item.get("accepted"))
        report["rejected"] += sum(1 for item in results if not item.get("accepted"))
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        out = Path(args.out).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(output, encoding="utf-8")
    print(f"table_quality_repair accepted={report['accepted']} rejected={report['rejected']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
