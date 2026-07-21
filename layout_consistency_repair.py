"""Conservative image-grounded repair for text assigned to the wrong visual panel."""

from __future__ import annotations

import base64
import copy
import io
import json
import os
import re
import unicodedata
from collections import Counter
from pathlib import Path

from PIL import Image


LAYOUT_CONSISTENCY_REPAIR = os.environ.get("LAYOUT_CONSISTENCY_REPAIR", "1") == "1"
LAYOUT_CONSISTENCY_REPAIR_MAX_PAGES = max(
    0, int(os.environ.get("LAYOUT_CONSISTENCY_REPAIR_MAX_PAGES", "8"))
)
LAYOUT_CONSISTENCY_REPAIR_MAX_TOKENS = max(
    1024, int(os.environ.get("LAYOUT_CONSISTENCY_REPAIR_MAX_TOKENS", "16384"))
)
LAYOUT_CONSISTENCY_REPAIR_TIMEOUT = max(
    60, int(os.environ.get("LAYOUT_CONSISTENCY_REPAIR_TIMEOUT", "600"))
)
LAYOUT_CONSISTENCY_REPAIR_IMG_MAXW = max(
    1024, int(os.environ.get("LAYOUT_CONSISTENCY_REPAIR_IMG_MAXW", "2464"))
)


VERIFY_SYSTEM = """You are a conservative visual QA checker for a document parser.
Compare the page image with the structured candidate. Check only material assignment and reading-order
errors among visible cards, panels, and sibling text blocks. A concrete error means that an exact visible
line or bullet is attached to the wrong panel, duplicated, or placed out of visual reading order.
Ignore typography, decorative icons, page-number metadata, and harmless whitespace.
When uncertain, pass the candidate. Return ONLY valid JSON:
{"pass": true|false, "reason": "short reason", "evidence": ["concrete image/candidate fact"]}"""


REPAIR_SYSTEM = """You repair structured text assignment on a visually complex document page.
Return ONLY one valid JSON object with the same page_number and the exact same element count and type
sequence as the supplied candidate.

Rules:
- Use the IMAGE as truth. The PDF text layer is noisy support only.
- Change only content fields of elements whose type is text.
- Keep every heading, table, figure, footnote, caption, description, and their order byte-for-byte unchanged.
- Preserve exactly the same visible character inventory across all text elements: move existing lines or
  bullets between text elements, but do not add, remove, rewrite, summarize, or duplicate any character.
- Assign each line to the card or panel whose visible border contains it and keep row-wise panel order.
- Output no markdown fences or commentary."""


def _normalized_inventory(data: dict) -> Counter:
    value = "".join(
        str(element.get("content") or "")
        for element in data.get("elements") or []
        if isinstance(element, dict) and element.get("type") == "text"
    )
    value = unicodedata.normalize("NFKC", value).casefold()
    return Counter(char for char in value if not char.isspace())


def _normalized_text_lines(data: dict) -> Counter:
    lines = []
    for element in data.get("elements") or []:
        if not isinstance(element, dict) or element.get("type") != "text":
            continue
        for line in re.split(
            r"(?:\r?\n)+|(?=[•◦▪■□※])",
            str(element.get("content") or ""),
        ):
            normalized = " ".join(
                unicodedata.normalize("NFKC", line).casefold().split()
            )
            normalized = re.sub(r"^[•◦▪■□※]\s*", "", normalized)
            if normalized:
                lines.append(normalized)
    return Counter(lines)


def _normalized_word_inventory(data: dict) -> Counter:
    """Preserve lexical content while allowing line and panel boundaries to move."""
    value = " ".join(
        str(element.get("content") or "")
        for element in data.get("elements") or []
        if isinstance(element, dict) and element.get("type") == "text"
    )
    value = unicodedata.normalize("NFKC", value).casefold()
    return Counter(re.findall(r"\w+", value, flags=re.UNICODE))


def _max_text_run(data: dict) -> int:
    longest = current = 0
    for element in data.get("elements") or []:
        if isinstance(element, dict) and element.get("type") == "text":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _raster_complexity(image_path: Path) -> tuple[float, float]:
    try:
        with Image.open(image_path) as source:
            image = source.convert("RGB")
            image.thumbnail((500, 700))
            grayscale = image.convert("L").tobytes()
            rgb = iter(image.tobytes())
    except Exception:
        return 0.0, 0.0
    total = max(1, len(grayscale))
    ink_fraction = sum(value < 245 for value in grayscale) / total
    colour_fraction = sum(
        max(red, green, blue) - min(red, green, blue) > 20 and gray < 245
        for (red, green, blue), gray in zip(zip(rgb, rgb, rgb), grayscale)
    ) / total
    return ink_fraction, colour_fraction


def _candidate_score(image_path: Path, data: dict) -> float | None:
    if _max_text_run(data) < 4 or len(data.get("elements") or []) < 5:
        return None
    ink_fraction, colour_fraction = _raster_complexity(image_path)
    if ink_fraction < 0.27 and colour_fraction < 0.10:
        return None
    return ink_fraction + colour_fraction


def _encode_image(image_path: Path) -> str:
    with Image.open(image_path) as source:
        image = source.convert("RGB")
        if image.width > LAYOUT_CONSISTENCY_REPAIR_IMG_MAXW:
            height = max(
                1,
                round(
                    image.height
                    * LAYOUT_CONSISTENCY_REPAIR_IMG_MAXW
                    / image.width
                ),
            )
            image = image.resize(
                (LAYOUT_CONSISTENCY_REPAIR_IMG_MAXW, height), Image.Resampling.LANCZOS
            )
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=92)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _public_candidate(data: dict) -> dict:
    return {
        "page_number": data.get("page_number"),
        "elements": [
            {
                key: element.get(key)
                for key in ("type", "content", "caption", "description")
                if element.get(key) not in (None, "")
            }
            for element in data.get("elements") or []
            if isinstance(element, dict)
        ],
    }


def _response_json(client, model: str, image_path: Path, system: str, user: str, max_tokens: int):
    image = _encode_image(image_path)
    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{image}"
                                },
                            },
                        ],
                    },
                ],
                temperature=0,
                max_tokens=max_tokens,
                timeout=LAYOUT_CONSISTENCY_REPAIR_TIMEOUT,
                response_format={"type": "json_object"},
                extra_body={"repetition_penalty": 1.05},
            )
            choice = response.choices[0]
            if getattr(choice, "finish_reason", None) == "length":
                continue
            raw = str(choice.message.content or "").strip()
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                match = re.search(r"\{.*\}", raw, re.S)
                if match:
                    try:
                        return json.loads(match.group(0))
                    except json.JSONDecodeError:
                        pass
        except Exception:
            if attempt:
                raise
    return None


def _reassignment_only(original: dict, candidate: dict) -> bool:
    if not isinstance(original, dict) or not isinstance(candidate, dict):
        return False
    old_elements = original.get("elements") or []
    new_elements = candidate.get("elements") or []
    if not isinstance(old_elements, list) or not isinstance(new_elements, list):
        return False
    if len(old_elements) != len(new_elements) or not old_elements:
        return False
    if not all(isinstance(element, dict) for element in old_elements + new_elements):
        return False
    if [element.get("type") for element in old_elements] != [
        element.get("type") for element in new_elements
    ]:
        return False
    changed = False
    for old, new in zip(old_elements, new_elements):
        if old.get("type") == "text":
            changed = changed or old.get("content") != new.get("content")
            for key in ("caption", "description"):
                if old.get(key) != new.get(key):
                    return False
            continue
        for key in ("content", "caption", "description"):
            if old.get(key) != new.get(key):
                return False
    return (
        changed
        and _normalized_inventory(original) == _normalized_inventory(candidate)
        and _normalized_word_inventory(original)
        == _normalized_word_inventory(candidate)
        and _normalized_text_lines(original) == _normalized_text_lines(candidate)
    )


def _merge_reassignment(original: dict, candidate: dict) -> dict | None:
    """Apply validated text moves while retaining parser metadata and evidence."""
    if not _reassignment_only(original, candidate):
        return None
    merged = copy.deepcopy(original)
    for old, new in zip(merged["elements"], candidate["elements"]):
        if old.get("type") == "text":
            old["content"] = new.get("content")
    return merged


def repair_layout_consistency(
    doc_output_dir: str, api_key: str, model: str, persist_page
) -> list[dict]:
    """Review a small set of panel-heavy pages and persist only proven reassignments."""
    if not LAYOUT_CONSISTENCY_REPAIR:
        return []
    from openai import OpenAI

    root = Path(doc_output_dir)
    candidates = []
    for structured_path in root.glob("page_*_structured.json"):
        image_path = structured_path.with_name(
            structured_path.name.replace("_structured.json", ".png")
        )
        if not image_path.exists():
            continue
        try:
            data = json.loads(structured_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        score = _candidate_score(image_path, data)
        if score is not None:
            candidates.append((score, structured_path, image_path, data))
    candidates.sort(key=lambda item: (-item[0], item[1].name))
    if LAYOUT_CONSISTENCY_REPAIR_MAX_PAGES > 0:
        candidates = candidates[:LAYOUT_CONSISTENCY_REPAIR_MAX_PAGES]
    if not candidates:
        return []

    client = OpenAI(
        base_url=os.environ.get("VLM_BASE_URL", "http://localhost:8000/v1"),
        api_key=api_key or "EMPTY",
        timeout=LAYOUT_CONSISTENCY_REPAIR_TIMEOUT,
        max_retries=0,
    )
    results = []
    for _, structured_path, image_path, original in candidates:
        stem = structured_path.name[: -len("_structured.json")]
        public = json.dumps(_public_candidate(original), ensure_ascii=False)
        record = {
            "page": original.get("page_number"),
            "accepted": False,
            "review": None,
        }
        try:
            verdict = _response_json(
                client,
                model,
                image_path,
                VERIFY_SYSTEM,
                f"Structured candidate:\n<candidate>{public}</candidate>",
                1024,
            )
        except Exception as exc:
            record["error"] = f"initial review failed: {type(exc).__name__}: {exc}"
            results.append(record)
            continue
        record["review"] = verdict
        if not isinstance(verdict, dict) or verdict.get("pass") is not False:
            results.append(record)
            continue
        text_path = structured_path.with_name(
            structured_path.name.replace("_structured.json", ".txt")
        )
        try:
            raw_text = text_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw_text = ""
        repair_user = (
            f"Visual QA failure: {json.dumps(verdict, ensure_ascii=False)}\n\n"
            f"Candidate:\n<candidate>{public}</candidate>\n\n"
            f"Noisy PDF text layer:\n<raw_text>{raw_text[:16000]}</raw_text>"
        )
        try:
            repaired = _response_json(
                client,
                model,
                image_path,
                REPAIR_SYSTEM,
                repair_user,
                LAYOUT_CONSISTENCY_REPAIR_MAX_TOKENS,
            )
        except Exception as exc:
            record["error"] = f"repair request failed: {type(exc).__name__}: {exc}"
            results.append(record)
            continue
        if not isinstance(repaired, dict):
            record["error"] = "repair returned invalid JSON"
            results.append(record)
            continue
        repaired = _merge_reassignment(original, repaired)
        if repaired is None:
            record["error"] = "repair was not a text-only inventory-preserving reassignment"
            results.append(record)
            continue
        final_public = json.dumps(_public_candidate(repaired), ensure_ascii=False)
        try:
            final_verdict = _response_json(
                client,
                model,
                image_path,
                VERIFY_SYSTEM,
                f"Structured candidate:\n<candidate>{final_public}</candidate>",
                1024,
            )
        except Exception as exc:
            record["error"] = f"final review failed: {type(exc).__name__}: {exc}"
            results.append(record)
            continue
        record["final_review"] = final_verdict
        if not isinstance(final_verdict, dict) or final_verdict.get("pass") is not True:
            record["error"] = "repaired candidate failed visual re-review"
            results.append(record)
            continue
        repaired["layout_consistency_repair"] = True
        persist_page(stem, json.dumps(repaired, ensure_ascii=False))
        record["accepted"] = True
        results.append(record)
    return results
