#!/usr/bin/env python3
"""Run VLM visual QA over parser output pages.

Use a VLM endpoint that was allocated through your cluster scheduler. This
script only calls an OpenAI-compatible endpoint; it does not start a GPU server.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import io
import json
import os
import re
import time
from pathlib import Path

from openai import OpenAI
from PIL import Image


SYSTEM = """You are a strict visual QA judge for document parsing.
Use the PAGE IMAGE as the source of truth. Compare it with the structured JSON.
Return ONLY valid JSON with this schema:
{
  "pass": true|false,
  "score": 0-100,
  "severity": "none|minor|major|critical",
  "issue_types": ["missing_text|wrong_text|wrong_order|table_structure|toc_structure|figure_description|hallucination|empty_or_low_confidence|other"],
  "missing_visible_text": ["short exact snippets"],
  "wrong_or_hallucinated_text": ["short snippets"],
  "reason": "short reason"
}

Pass only if important visible text and document structure are preserved.
Fail for missing headings, missing list/table rows, wrong TOC page numbers,
materially wrong table structure, empty/low-confidence fallback warnings,
hallucinated content, or wrong reading order.
Ignore decorative backgrounds, leader dots in a TOC, and tiny repeated
page headers/footers unless they are the only content.
For tables, exact HTML tags may differ, but cells/rows/headers and merged-cell
meaning must be preserved. For figures/screenshots/charts, visible labels/data
and a reasonable description must be present."""


def encode_image(path: str, max_width: int) -> str:
    with Image.open(path) as img:
        if img.width > max_width:
            ratio = max_width / img.width
            img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def strip_fences(text: str | None) -> str:
    text = (text or "").strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def first_json_obj(text: str | None) -> dict | None:
    raw = strip_fences(text)
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for idx, ch in enumerate(raw[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(raw[start : idx + 1])
                    return obj if isinstance(obj, dict) else None
                except Exception:
                    return None
    return None


def norm_text(text: object) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", str(text or "")).lower()


def structured_plain_text(path: str) -> str:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return ""
    parts = []
    for elem in data.get("elements") or []:
        if not isinstance(elem, dict):
            continue
        for key in ("content", "caption", "description"):
            if elem.get(key):
                parts.append(str(elem.get(key)))
    return "\n".join(parts)


def stabilize_verdict(verdict: dict, structured_text: str) -> dict:
    """Reduce obvious VLM-judge false positives without hiding hard failures."""
    verdict = dict(verdict or {})
    issues = list(verdict.get("issue_types") or [])
    missing = list(verdict.get("missing_visible_text") or [])
    wrong = list(verdict.get("wrong_or_hallucinated_text") or [])
    nstruct = norm_text(structured_text)
    missing = [item for item in missing if norm_text(item) and norm_text(item) not in nstruct]
    wrong = [item for item in wrong if norm_text(item) and norm_text(item) not in nstruct]
    if not missing and not wrong:
        issues = [item for item in issues if item not in ("wrong_text", "missing_text")]
    score = int(verdict.get("score", 0) or 0)
    soft_only = set(issues).issubset({"wrong_order", "other", "figure_description", "table_structure"})
    if (not missing and not wrong and score >= 85 and soft_only) or (not issues and score >= 80):
        verdict["pass"] = True
        verdict["severity"] = "minor" if issues else "none"
    verdict["missing_visible_text"] = missing
    verdict["wrong_or_hallucinated_text"] = wrong
    verdict["issue_types"] = issues
    return verdict


def compact_structured(path: str, limit: int) -> str:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        text = json.dumps(data, ensure_ascii=False)
    except Exception:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n...[middle omitted for judge]...\n" + text[-half:]


def page_number_from_name(name: str) -> int | None:
    match = re.search(r"page_(\d+)", name)
    return int(match.group(1)) if match else None


def collect_pages(root: Path) -> list[dict]:
    pages = []
    for doc_dir in sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith("_")):
        for img in sorted(doc_dir.glob("page_*.png")):
            stem = img.stem
            structured = doc_dir / f"{stem}_structured.json"
            pages.append(
                {
                    "doc": doc_dir.name,
                    "page": page_number_from_name(stem),
                    "image": str(img),
                    "structured": str(structured) if structured.exists() else None,
                }
            )
    return pages


def judge_one(client: OpenAI, model: str, item: dict, args: argparse.Namespace) -> dict:
    started = time.time()
    result = {
        "doc": item["doc"],
        "page": item["page"],
        "image": item["image"],
        "structured": item["structured"],
        "pass": False,
        "score": 0,
        "severity": "critical",
        "issue_types": ["empty_or_low_confidence"],
        "missing_visible_text": [],
        "wrong_or_hallucinated_text": [],
        "reason": "structured json missing",
        "seconds": None,
    }
    if not item["structured"] or not os.path.exists(item["structured"]):
        result["seconds"] = round(time.time() - started, 2)
        return result

    structured = compact_structured(item["structured"], args.structured_limit)
    user = (
        "Structured JSON candidate:\n"
        "<structured_json>\n"
        f"{structured}\n"
        "</structured_json>\n\n"
        "Judge this page against the image. Use a concise Korean reason when the document is Korean."
    )
    b64 = encode_image(item["image"], args.max_width)
    last_error = None
    for attempt in range(args.retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        ],
                    },
                ],
                temperature=0,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
                extra_body={"repetition_penalty": 1.05},
            )
            verdict = first_json_obj(resp.choices[0].message.content)
            if verdict is None:
                raise ValueError("judge returned no parseable JSON object")
            verdict = stabilize_verdict(verdict, structured_plain_text(item["structured"]))
            result.update(
                {
                    "pass": bool(verdict.get("pass")),
                    "score": int(verdict.get("score", 0)),
                    "severity": verdict.get("severity") or ("none" if verdict.get("pass") else "major"),
                    "issue_types": verdict.get("issue_types") or [],
                    "missing_visible_text": verdict.get("missing_visible_text") or [],
                    "wrong_or_hallucinated_text": verdict.get("wrong_or_hallucinated_text") or [],
                    "reason": verdict.get("reason") or "",
                }
            )
            result["seconds"] = round(time.time() - started, 2)
            return result
        except Exception as exc:
            last_error = str(exc)
            time.sleep(2 * (attempt + 1))
    result["reason"] = f"judge error: {last_error}"
    result["seconds"] = round(time.time() - started, 2)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir")
    parser.add_argument("--out")
    parser.add_argument("--model", default=os.environ.get("JUDGE_MODEL", "Qwen/Qwen3-VL-30B-A3B-Instruct"))
    parser.add_argument("--base-url", default=os.environ.get("VLM_BASE_URL", "http://127.0.0.1:8000/v1"))
    parser.add_argument("--workers", type=int, default=int(os.environ.get("JUDGE_WORKERS", "4")))
    parser.add_argument("--max-width", type=int, default=int(os.environ.get("JUDGE_IMG_MAXW", "1400")))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("JUDGE_TIMEOUT", "180")))
    parser.add_argument("--max-tokens", type=int, default=int(os.environ.get("JUDGE_MAX_TOKENS", "768")))
    parser.add_argument("--structured-limit", type=int, default=int(os.environ.get("JUDGE_STRUCTURED_LIMIT", "14000")))
    parser.add_argument("--retries", type=int, default=int(os.environ.get("JUDGE_RETRIES", "1")))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--pages", default="", help="Comma/space list of page numbers to judge")
    parser.add_argument("--docs", default="", help="Regex filter for document directory names")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_dir = Path(args.run_dir)
    out_dir = Path(args.out) if args.out else run_dir / "_visual_judge"
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "visual_judge_pages.jsonl"
    summary_path = out_dir / "visual_judge_summary.json"

    pages = collect_pages(run_dir)
    if args.docs:
        doc_rx = re.compile(args.docs)
        pages = [page for page in pages if doc_rx.search(page["doc"])]
    if args.pages:
        wanted = {int(item) for item in re.split(r"[,\s]+", args.pages.strip()) if item}
        pages = [page for page in pages if page["page"] in wanted]
    if args.limit:
        pages = pages[: args.limit]

    client = OpenAI(base_url=args.base_url, api_key=os.environ.get("VLM_API_KEY", "EMPTY"), timeout=args.timeout, max_retries=0)
    started = time.time()
    results = []
    with jsonl_path.open("w", encoding="utf-8") as jf:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(judge_one, client, args.model, item, args) for item in pages]
            for idx, future in enumerate(concurrent.futures.as_completed(futures), 1):
                item = future.result()
                results.append(item)
                jf.write(json.dumps(item, ensure_ascii=False) + "\n")
                jf.flush()
                status = "PASS" if item["pass"] else "FAIL"
                print(f"[{idx}/{len(pages)}] {status} {item['doc']} p{item['page']}: {item['reason']}", flush=True)

    failed = [item for item in results if not item["pass"]]
    by_doc = {}
    for item in results:
        bucket = by_doc.setdefault(item["doc"], {"total": 0, "pass": 0, "fail": 0, "issues": []})
        bucket["total"] += 1
        if item["pass"]:
            bucket["pass"] += 1
        else:
            bucket["fail"] += 1
            bucket["issues"].append(
                {
                    "page": item["page"],
                    "severity": item["severity"],
                    "score": item["score"],
                    "issue_types": item["issue_types"],
                    "reason": item["reason"],
                    "missing_visible_text": item["missing_visible_text"][:8],
                    "wrong_or_hallucinated_text": item["wrong_or_hallucinated_text"][:8],
                }
            )

    summary = {
        "run_dir": str(run_dir),
        "model": args.model,
        "base_url": args.base_url,
        "total_pages": len(results),
        "pass": len(results) - len(failed),
        "fail": len(failed),
        "seconds": round(time.time() - started, 1),
        "by_doc": by_doc,
        "failed_pages": failed,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nsummary={summary_path} pass={summary['pass']}/{summary['total_pages']} fail={summary['fail']}", flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
