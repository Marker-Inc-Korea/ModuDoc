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
import unicodedata
from pathlib import Path

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
  "text_mismatches": [{"image_text": "exact image text", "candidate_text": "exact candidate text"}],
  "hallucinated_candidate_text": ["exact candidate-only snippets"],
  "structure_evidence": ["specific image-vs-candidate fact, e.g. image has 6 columns but candidate has 5"],
  "reason": "short reason"
}

Pass only if important visible text and document structure are preserved.
Fail for missing headings, missing list/table rows, wrong TOC page numbers,
materially wrong table structure, empty/low-confidence fallback warnings,
hallucinated content, or wrong reading order.
Ignore decorative backgrounds, leader dots in a TOC, and tiny repeated
page headers/footers unless they are the only content.
Ignore differences limited to whitespace, line wrapping, punctuation style,
HTML tag choice, or whether a title is a heading versus a caption. These are
not wrong_text or table_structure failures when content and meaning match.
Also ignore colors, borders, font size/weight, centering, exact coordinates,
and whether visible content is represented as text versus a figure body. The
schema does not encode visual styling. Judge reading order and semantic
structure, not visual presentation.
For tables, exact HTML tags may differ, but cells/rows/headers and merged-cell
meaning must be preserved. For figures/screenshots/charts, visible labels/data
and a reasonable description must be present.

Evidence is mandatory for a failure:
- Every wrong_text claim needs an image_text/candidate_text pair. Never cite
  candidate text by itself. The normalized pair must actually differ.
- Every hallucination claim needs an exact candidate-only snippet.
- Every wrong_order/table_structure/toc_structure claim needs concrete
  structure_evidence describing both image and candidate, including short
  candidate snippets in their actual JSON sequence. Read that sequence before
  claiming an ordering difference.
- Text present in any candidate element is not missing merely because its
  element type differs. Do not require a figure description to repeat text
  already preserved in separate text/table elements.
- Do not return pass=false with score >= 90 for cosmetic differences. If your
  reason says the structure/content is accurate, pass must be true."""


REVIEW_SYSTEM = """You are the second-pass adjudicator for document parser QA.
Use the PAGE IMAGE and structured JSON as the source of truth. Perform an
independent blind check of ONLY the supplied issue categories. The first judge
may be wrong; do not assume that any supplied category is present.

Return ONLY valid JSON:
{
  "confirmed_failure": true|false,
  "confirmed_issue_types": ["missing_text|wrong_text|wrong_order|table_structure|toc_structure|figure_description|hallucination|empty_or_low_confidence|other"],
  "missing_visible_text": ["short exact image snippets absent from candidate"],
  "text_mismatches": [{"image_text": "exact image text", "candidate_text": "exact candidate text"}],
  "hallucinated_candidate_text": ["exact candidate-only snippets"],
  "structure_evidence": ["specific image-vs-candidate structural fact"],
  "evidence": ["specific non-text evidence for figure_description or other"],
  "rejected_claims": ["short explanation"],
  "reason": "short final reason"
}

Confirm a category only when you independently observe a material error and
can fill its corresponding evidence field. Reject claims based only on color,
borders, font, centering, exact
coordinates, heading-vs-caption choice, or text-vs-figure element type. Text is
not missing if it appears anywhere in the candidate. For order claims, read the
actual candidate element sequence and compare it with the image.
For table claims, verify visible row/column/merged-cell meaning, not HTML style.
Whitespace and punctuation-style variants are not wrong_text. Page counters,
repeated headers, and repeated footers are not missing_text.
When evidence is ambiguous, set confirmed_failure=false rather than guessing."""


OBJECTIVE_QUALITY_ISSUES = {
    "no_table",
    "empty_table",
    "ragged_rows",
    "nested_table",
    "quality_check_error",
}


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
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(char for char in value if char.isalnum())


def looks_like_page_artifact(text: object) -> bool:
    """Identify standalone page counters, without suppressing ordinary numbers."""
    value = str(text or "").strip().casefold()
    if not value:
        return False
    if re.fullmatch(r"(?:page|p\.?|페이지|쪽)\s*\d{1,4}(?:\s*(?:/|of)\s*\d{1,4})?", value):
        return True
    if not re.search(r"[-–—_/|·.]", value):
        return False
    return bool(
        re.fullmatch(
            r"[\s\-–—_/|·.()\[\]]*\d{1,4}"
            r"(?:\s*(?:/|of)\s*\d{1,4})?[\s\-–—_/|·.()\[\]]*",
            value,
        )
    )


def looks_like_page_artifact_evidence(text: object) -> bool:
    value = str(text or "").casefold()
    return any(
        marker in value
        for marker in (
            "page number",
            "page-number",
            "pagination",
            "printed page",
            "페이지 번호",
            "쪽 번호",
            "인쇄 번호",
        )
    )


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


def objective_quality_failures(data: dict) -> list[str]:
    """Return parser-emitted hard quality signals that a VLM cannot overturn."""
    failures = []
    if data.get("low_confidence"):
        failures.append("page_low_confidence")
    for index, element in enumerate(data.get("elements") or []):
        if not isinstance(element, dict):
            continue
        confidence = element.get("_confidence")
        try:
            if confidence is not None and float(confidence) < 0.75:
                failures.append(f"element_{index}_low_confidence")
        except (TypeError, ValueError):
            failures.append(f"element_{index}_invalid_confidence")
        for issue in element.get("_issues") or []:
            if isinstance(issue, (list, tuple)):
                issue = issue[0] if issue else ""
            issue_name = str(issue or "").split(":", 1)[0]
            if issue_name in OBJECTIVE_QUALITY_ISSUES:
                failures.append(f"element_{index}_{issue_name}")
    return sorted(set(failures))


def stabilize_verdict(verdict: dict, structured_text: str) -> dict:
    """Reduce obvious VLM-judge false positives without hiding hard failures."""
    verdict = dict(verdict or {})
    issues = list(verdict.get("issue_types") or [])
    missing = list(verdict.get("missing_visible_text") or [])
    mismatches = list(verdict.get("text_mismatches") or [])
    hallucinated = list(verdict.get("hallucinated_candidate_text") or [])
    structure_evidence = [
        str(item).strip()
        for item in (verdict.get("structure_evidence") or [])
        if str(item).strip() and not looks_like_page_artifact_evidence(item)
    ]
    nstruct = norm_text(structured_text)
    missing = [
        item for item in missing
        if norm_text(item)
        and norm_text(item) not in nstruct
        and not looks_like_page_artifact(item)
    ]
    verified_mismatches = []
    for item in mismatches:
        if not isinstance(item, dict):
            continue
        image_text = str(item.get("image_text") or "").strip()
        candidate_text = str(item.get("candidate_text") or "").strip()
        nimage = norm_text(image_text)
        ncandidate = norm_text(candidate_text)
        if (
            nimage
            and ncandidate
            and nimage != ncandidate
            and ncandidate in nstruct
            and not (
                looks_like_page_artifact(image_text)
                and looks_like_page_artifact(candidate_text)
            )
        ):
            verified_mismatches.append(
                {"image_text": image_text, "candidate_text": candidate_text}
            )
    hallucinated = [
        item for item in hallucinated
        if norm_text(item)
        and norm_text(item) in nstruct
        and not looks_like_page_artifact(item)
    ]
    if not missing:
        issues = [item for item in issues if item != "missing_text"]
    if not verified_mismatches:
        issues = [item for item in issues if item != "wrong_text"]
    if not hallucinated:
        issues = [item for item in issues if item != "hallucination"]
    structural_types = {"wrong_order", "table_structure", "toc_structure"}
    if not structure_evidence:
        issues = [item for item in issues if item not in structural_types]
    score = int(verdict.get("score", 0) or 0)
    evidenced_structure = bool(set(issues) & structural_types and structure_evidence)
    hard_metadata = "empty_or_low_confidence" in issues
    soft_only = set(issues).issubset({"other", "figure_description"})
    hard_detected = bool(
        missing or verified_mismatches or hallucinated or evidenced_structure or hard_metadata
    )
    if hard_detected:
        verdict["pass"] = False
        if verdict.get("severity") in (None, "none", "minor"):
            verdict["severity"] = "major"
    elif (
        (not missing and not verified_mismatches and not hallucinated and score >= 85 and soft_only)
        or not issues
    ):
        verdict["pass"] = True
        verdict["severity"] = "minor" if issues else "none"
        if not issues:
            verdict["score"] = max(80, score)
    verdict["missing_visible_text"] = missing
    verdict["text_mismatches"] = verified_mismatches
    verdict["hallucinated_candidate_text"] = hallucinated
    verdict["structure_evidence"] = structure_evidence
    verdict["wrong_or_hallucinated_text"] = [
        item["candidate_text"] for item in verified_mismatches
    ] + hallucinated
    verdict["issue_types"] = issues
    return verdict


def validated_review_issue_types(review: dict, structured_text: str = "") -> list[str]:
    """Accept review categories only when their required evidence is checkable."""
    raw_types = list((review or {}).get("confirmed_issue_types") or [])
    nstruct = norm_text(structured_text)
    valid = set()

    missing = [
        item for item in ((review or {}).get("missing_visible_text") or [])
        if norm_text(item)
        and norm_text(item) not in nstruct
        and not looks_like_page_artifact(item)
    ]
    if missing:
        valid.add("missing_text")

    for item in (review or {}).get("text_mismatches") or []:
        if not isinstance(item, dict):
            continue
        image_text = str(item.get("image_text") or "").strip()
        candidate_text = str(item.get("candidate_text") or "").strip()
        nimage = norm_text(image_text)
        ncandidate = norm_text(candidate_text)
        if (
            nimage
            and ncandidate
            and nimage != ncandidate
            and ncandidate in nstruct
            and not (
                looks_like_page_artifact(image_text)
                and looks_like_page_artifact(candidate_text)
            )
        ):
            valid.add("wrong_text")
            break

    hallucinated = [
        item for item in ((review or {}).get("hallucinated_candidate_text") or [])
        if norm_text(item)
        and norm_text(item) in nstruct
        and not looks_like_page_artifact(item)
    ]
    if hallucinated:
        valid.add("hallucination")

    structure_evidence = [
        str(item).strip()
        for item in ((review or {}).get("structure_evidence") or [])
        if str(item).strip() and not looks_like_page_artifact_evidence(item)
    ]
    if structure_evidence:
        valid.update(
            item
            for item in raw_types
            if item in {"wrong_order", "table_structure", "toc_structure"}
        )

    generic_evidence = [
        str(item).strip() for item in ((review or {}).get("evidence") or [])
        if str(item).strip()
    ]
    if generic_evidence:
        valid.update(
            item for item in raw_types if item in {"figure_description", "other"}
        )
    return [item for item in raw_types if item in valid]


def apply_failure_review(primary: dict, review: dict, structured_text: str = "") -> dict:
    """Merge an adjudication while retaining the primary verdict separately."""
    result = dict(primary or {})
    primary_types = list(result.get("issue_types") or [])
    reviewed_types = [
        item for item in validated_review_issue_types(review, structured_text)
        if item in primary_types
    ]
    confirmed = bool((review or {}).get("confirmed_failure") and reviewed_types)
    if confirmed:
        if reviewed_types:
            result["issue_types"] = reviewed_types
        if "missing_text" not in reviewed_types:
            result["missing_visible_text"] = []
        if "wrong_text" not in reviewed_types:
            result["text_mismatches"] = []
        if "hallucination" not in reviewed_types:
            result["hallucinated_candidate_text"] = []
        if not set(reviewed_types) & {"wrong_order", "table_structure", "toc_structure"}:
            result["structure_evidence"] = []
        result["wrong_or_hallucinated_text"] = [
            item.get("candidate_text", "")
            for item in (result.get("text_mismatches") or [])
            if isinstance(item, dict) and item.get("candidate_text")
        ] + list(result.get("hallucinated_candidate_text") or [])
        result["pass"] = False
        if result.get("severity") in (None, "none", "minor"):
            result["severity"] = "major"
    else:
        result.update(
            {
                "pass": True,
                "score": max(85, int(result.get("score", 0) or 0)),
                "severity": "none",
                "issue_types": [],
                "missing_visible_text": [],
                "text_mismatches": [],
                "hallucinated_candidate_text": [],
                "structure_evidence": [],
                "wrong_or_hallucinated_text": [],
            }
        )
    if confirmed and (review or {}).get("reason"):
        result["reason"] = str(review["reason"])
    elif not (review or {}).get("confirmed_failure") and (review or {}).get("reason"):
        result["reason"] = str(review["reason"])
    elif not confirmed:
        result["reason"] = "Second-pass review supplied no valid evidence for a primary failure."
    return result


def review_failure(client, model: str, structured: str, primary: dict, b64: str,
                   args: argparse.Namespace) -> dict | None:
    user = (
        "Structured JSON candidate:\n"
        "<structured_json>\n"
        f"{structured}\n"
        "</structured_json>\n\n"
        "Issue categories flagged by another system (claims and reasons are intentionally hidden):\n"
        "<claimed_issue_types>\n"
        f"{json.dumps(primary.get('issue_types') or [], ensure_ascii=False)}\n"
        "</claimed_issue_types>\n\n"
        "Independently determine whether any supplied category is materially true."
    )
    for attempt in range(args.retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": REVIEW_SYSTEM},
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                            {"type": "text", "text": user},
                        ],
                    },
                ],
                temperature=0,
                max_tokens=args.review_max_tokens,
                timeout=args.review_timeout,
                response_format={"type": "json_object"},
                extra_body={"repetition_penalty": 1.08, "no_repeat_ngram_size": 16},
            )
            verdict = first_json_obj(resp.choices[0].message.content)
            if verdict is not None:
                return verdict
        except Exception:
            pass
        if attempt < args.retries:
            time.sleep(attempt + 1)
    return None


def compact_structured(path: str, limit: int) -> str:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        compact = {
            key: data.get(key)
            for key in ("page_number", "low_confidence", "warning")
            if data.get(key) not in (None, "", False)
        }
        compact["elements"] = []
        for element in data.get("elements") or []:
            if not isinstance(element, dict):
                continue
            compact["elements"].append(
                {
                    key: element.get(key)
                    for key in ("type", "content", "caption", "description")
                    if element.get(key) not in (None, "")
                }
            )
        text = json.dumps(compact, ensure_ascii=False)
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


def judge_one(client, model: str, item: dict, args: argparse.Namespace) -> dict:
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
        "text_mismatches": [],
        "hallucinated_candidate_text": [],
        "structure_evidence": [],
        "wrong_or_hallucinated_text": [],
        "reason": "structured json missing",
        "primary_pass": False,
        "primary_verdict": None,
        "reviewed": False,
        "review_verdict": None,
        "seconds": None,
    }
    if not item["structured"] or not os.path.exists(item["structured"]):
        result["seconds"] = round(time.time() - started, 2)
        return result

    try:
        structured_data = json.loads(
            Path(item["structured"]).read_text(encoding="utf-8")
        )
    except Exception:
        structured_data = {}
    objective_failures = objective_quality_failures(structured_data)
    if objective_failures:
        result.update(
            {
                "pass": False,
                "score": 0,
                "severity": "critical",
                "issue_types": ["empty_or_low_confidence"],
                "reason": "Parser metadata reports objective low-confidence or malformed structure.",
                "primary_pass": False,
                "primary_verdict": {
                    "pass": False,
                    "issue_types": ["empty_or_low_confidence"],
                    "objective_quality_failures": objective_failures,
                },
                "seconds": round(time.time() - started, 2),
            }
        )
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
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                            {"type": "text", "text": user},
                        ],
                    },
                ],
                temperature=0,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
                response_format={"type": "json_object"},
                extra_body={"repetition_penalty": 1.08, "no_repeat_ngram_size": 16},
            )
            verdict = first_json_obj(resp.choices[0].message.content)
            if verdict is None:
                raise ValueError("judge returned no parseable JSON object")
            plain_text = structured_plain_text(item["structured"])
            verdict = stabilize_verdict(verdict, plain_text)
            primary = dict(verdict)
            result["primary_pass"] = bool(primary.get("pass"))
            result["primary_verdict"] = primary
            if (
                not primary.get("pass")
                and args.review_failures
                and "empty_or_low_confidence" not in (primary.get("issue_types") or [])
            ):
                review = review_failure(client, model, structured, primary, b64, args)
                if review is not None:
                    result["reviewed"] = True
                    result["review_verdict"] = review
                    verdict = apply_failure_review(primary, review, plain_text)
            result.update(
                {
                    "pass": bool(verdict.get("pass")),
                    "score": int(verdict.get("score", 0)),
                    "severity": verdict.get("severity") or ("none" if verdict.get("pass") else "major"),
                    "issue_types": verdict.get("issue_types") or [],
                    "missing_visible_text": verdict.get("missing_visible_text") or [],
                    "text_mismatches": verdict.get("text_mismatches") or [],
                    "hallucinated_candidate_text": verdict.get("hallucinated_candidate_text") or [],
                    "structure_evidence": verdict.get("structure_evidence") or [],
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
    parser.add_argument("--max-tokens", type=int, default=int(os.environ.get("JUDGE_MAX_TOKENS", "1536")))
    parser.add_argument("--structured-limit", type=int, default=int(os.environ.get("JUDGE_STRUCTURED_LIMIT", "14000")))
    parser.add_argument("--retries", type=int, default=int(os.environ.get("JUDGE_RETRIES", "1")))
    parser.add_argument(
        "--review-failures",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("JUDGE_REVIEW_FAILURES", "1") == "1",
    )
    parser.add_argument(
        "--review-max-tokens",
        type=int,
        default=int(os.environ.get("JUDGE_REVIEW_MAX_TOKENS", "1536")),
    )
    parser.add_argument(
        "--review-timeout",
        type=int,
        default=int(os.environ.get("JUDGE_REVIEW_TIMEOUT", "240")),
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--pages", default="", help="Comma/space list of page numbers to judge")
    parser.add_argument("--docs", default="", help="Regex filter for document directory names")
    return parser.parse_args()


def main() -> int:
    from openai import OpenAI

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
                    "text_mismatches": item["text_mismatches"][:8],
                    "structure_evidence": item["structure_evidence"][:8],
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
        "primary_fail": sum(not item.get("primary_pass", item["pass"]) for item in results),
        "reviewed": sum(bool(item.get("reviewed")) for item in results),
        "review_overturned": sum(
            bool(item.get("reviewed")) and not item.get("primary_pass") and item.get("pass")
            for item in results
        ),
        "seconds": round(time.time() - started, 1),
        "by_doc": by_doc,
        "failed_pages": failed,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nsummary={summary_path} pass={summary['pass']}/{summary['total_pages']} fail={summary['fail']}", flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
