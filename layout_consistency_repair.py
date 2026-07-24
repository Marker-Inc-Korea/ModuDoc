"""Conservative image-grounded repair for text assigned to the wrong visual panel."""

from __future__ import annotations

import base64
import copy
import io
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict, deque
from difflib import SequenceMatcher
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
Each candidate element has an explicit zero-based index and is an independent record. Never infer that
one text element is attached to the preceding or following text element merely because they are adjacent
in the JSON array. For bounded peer cards, use row-major order: top-left, top-right, then the next row.
First count the visible card grid as ROWS top-to-bottom and COLUMNS left-to-right; do not transpose those
counts. A standalone text element whose first line is a visible card title is the expected representation
of that card, not an ordering error. Ignore duplicated representation between text elements and an
immutable table element; table/text deduplication is outside this audit.
Fail a text-card assignment only when an exact subordinate line from candidate element index N is visibly
inside a differently titled bounded card. Cite both the candidate's card title and the destination card
title. Do not propose moving a correctly anchored card title merely because one of its bullets is missing.
When failing a candidate, cite the affected element as exactly "candidate element index N", quote a short
candidate snippet, and name the visible card that contains the line. An order failure must cite at least
two candidate indexes and their visible row/column positions. When uncertain or unable to provide this
grounding, pass the candidate. Return ONLY valid JSON:
{"pass": true|false, "reason": "short reason", "evidence": ["indexed image/candidate fact"]}"""


FINAL_VERIFY_SYSTEM = """You are a conservative visual panel-mapping auditor for a document parser.
Use the page IMAGE as truth. The candidate elements are independent records with explicit zero-based
indexes; JSON adjacency never means that one element is attached to another. Audit ONLY the supplied
changed text element indexes. For each changed element, locate the single bounded card or panel containing
its visible title and body, then report its one-based row and column. Check that every line in that candidate
element belongs inside that same visible card. For bounded peer cards, reading order is row-major:
top-left, top-right, then the next row. Ignore typography, icons, whitespace, and unchanged elements.

Return ONLY valid JSON with this schema:
{
  "pass": true|false,
  "reading_order_matches": true|false,
  "changed_elements": [
    {
      "candidate_index": 0,
      "visible_card_title": "exact short visible title",
      "image_row": 1,
      "image_column": 1,
      "content_matches_visible_card": true|false,
      "evidence": "short exact image/candidate fact"
    }
  ],
  "reason": "short reason"
}
Include exactly one changed_elements entry for every supplied changed index and no others. Set pass=true
only when every content_matches_visible_card value and reading_order_matches are true. When uncertain,
set pass=false."""


REPAIR_SYSTEM = """You repair structured text assignment on a visually complex document page.
Return ONLY one valid JSON object with the same page_number and the exact same element count and type
sequence as the supplied candidate.

Rules:
- Use the IMAGE as truth. The PDF text layer is noisy support only.
- Change only content fields of elements whose type is text.
- Keep every heading, table, figure, footnote, caption, description, and their order byte-for-byte unchanged.
- Ignore any review claim about duplication between text and an immutable table; this repair only moves
  text between bounded peer cards.
- When a text element's first non-bullet line exactly matches its visible card title, keep that title as the
  element's anchor. Move only subordinate lines or bullets that visibly belong under another anchored title.
- Preserve exactly the same visible character inventory across all text elements: move existing lines or
  bullets between text elements, but do not add, remove, rewrite, summarize, or duplicate any character.
- Assign each line to the card or panel whose visible border contains it and keep row-wise panel order.
- Output no markdown fences or commentary."""


SEGMENT_ASSIGN_SYSTEM = """You assign immutable text segments to visibly bounded peer cards.
Return ONLY one valid JSON object:
{"assignments": [{"segment_id": "e1s1", "candidate_index": 1}]}

Rules:
- Use the image as truth and the supplied card-title anchors as destinations.
- Assign every supplied segment_id exactly once to one supplied candidate_index.
- A segment belongs to the card whose visible border contains that exact line or bullet.
- Do not assign or rewrite card titles. Do not edit segment text. Do not discuss tables.
- Count rows top-to-bottom and columns left-to-right; preserve row-major card order.
- Output no extra keys, prose, or markdown."""


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


def _atomic_text_segments(content: object) -> list[tuple[tuple[str, ...], str]]:
    """Split movable text while retaining the parser's exact source spelling."""
    segments = []
    for raw in re.split(
        r"(?:\r?\n)+|(?=[•◦▪■□※])",
        str(content or ""),
    ):
        source = raw.strip()
        signature = tuple(
            re.findall(
                r"\w+",
                unicodedata.normalize("NFKC", source).casefold(),
                flags=re.UNICODE,
            )
        )
        if source:
            segments.append((signature, source))
    return segments


def _card_segment_spec(data: dict) -> dict | None:
    """Select one unambiguous run of independently titled peer-card texts."""
    elements = data.get("elements") or []
    runs = []
    current = []
    for index, element in enumerate(elements):
        if isinstance(element, dict) and element.get("type") == "text":
            current.append((index, element))
            continue
        if current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)

    eligible = []
    for run in runs:
        if not (4 <= len(run) <= 12):
            continue
        parsed = [
            (index, _atomic_text_segments(element.get("content")))
            for index, element in run
        ]
        if any(not segments for _, segments in parsed):
            continue
        titles = [segments[0][0] for _, segments in parsed]
        if any(not title for title in titles) or len(set(titles)) != len(titles):
            continue
        if sum(len(segments) > 1 for _, segments in parsed) * 2 < len(parsed):
            continue

        anchors = []
        segments = []
        source_order = 0
        for index, parts in parsed:
            anchors.append(
                {
                    "candidate_index": index,
                    "title": parts[0][1],
                }
            )
            for segment_index, (_, source) in enumerate(parts[1:], 1):
                segments.append(
                    {
                        "segment_id": f"e{index}s{segment_index}",
                        "source_index": index,
                        "source_order": source_order,
                        "text": source,
                    }
                )
                source_order += 1
        if len(segments) >= 2:
            eligible.append({"anchors": anchors, "segments": segments})
    return eligible[0] if len(eligible) == 1 else None


def _segment_assignment_user_prompt(spec: dict) -> str:
    public = {
        "card_title_anchors": spec["anchors"],
        "immutable_segments": [
            {"segment_id": item["segment_id"], "text": item["text"]}
            for item in spec["segments"]
        ],
    }
    return (
        "Map every immutable segment to the bounded card that visibly contains "
        "it. Candidate indexes refer only to the supplied title anchors.\n\n"
        f"<assignment_input>{json.dumps(public, ensure_ascii=False)}</assignment_input>"
    )


def _apply_card_segment_assignment(
    original: dict, spec: dict, response: object
) -> dict | None:
    if not isinstance(response, dict) or not isinstance(
        response.get("assignments"), list
    ):
        return None
    expected = {item["segment_id"]: item for item in spec["segments"]}
    destinations = {item["candidate_index"] for item in spec["anchors"]}
    assignments = {}
    for item in response["assignments"]:
        if not isinstance(item, dict):
            return None
        segment_id = item.get("segment_id")
        destination = item.get("candidate_index")
        if (
            type(segment_id) is not str
            or type(destination) is not int
            or segment_id not in expected
            or destination not in destinations
            or segment_id in assignments
        ):
            return None
        assignments[segment_id] = destination
    if set(assignments) != set(expected):
        return None

    candidate = copy.deepcopy(original)
    by_destination = defaultdict(list)
    for segment_id, destination in assignments.items():
        by_destination[destination].append(expected[segment_id])
    for anchor in spec["anchors"]:
        destination = anchor["candidate_index"]
        parts = [anchor["title"]]
        parts.extend(
            item["text"]
            for item in sorted(
                by_destination[destination], key=lambda value: value["source_order"]
            )
        )
        candidate["elements"][destination]["content"] = "\n".join(parts)
    merged = _merge_reassignment(original, candidate)
    if merged is None or not _changed_text_indexes(original, merged):
        return None
    return merged


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
                "index": index,
                **{
                    key: element.get(key)
                    for key in ("type", "content", "caption", "description")
                    if element.get(key) not in (None, "")
                },
            }
            for index, element in enumerate(data.get("elements") or [])
            if isinstance(element, dict)
        ],
    }


def _repair_user_prompt(public: str, raw_text: str) -> str:
    """Keep an unreliable initial review from steering the constrained repair."""
    return (
        "An initial visual checker found a possible card-assignment error, but its "
        "explanation may contain out-of-scope claims. Re-evaluate the image "
        "independently under the system rules. Ignore claims about standalone "
        "elements, table/text duplication, or transposed row/column counts. Move "
        "only exact subordinate lines that visibly belong under another anchored "
        "card title.\n\n"
        f"Candidate:\n<candidate>{public}</candidate>\n\n"
        f"Noisy PDF text layer:\n<raw_text>{raw_text[:16000]}</raw_text>"
    )


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


def _canonicalized_reassignment(original: dict, candidate: dict) -> dict | None:
    """Replace VLM list-marker rewrites with the exact original text segments."""
    if not isinstance(original, dict) or not isinstance(candidate, dict):
        return None
    old_elements = original.get("elements") or []
    new_elements = candidate.get("elements") or []
    if (
        not isinstance(old_elements, list)
        or not isinstance(new_elements, list)
        or len(old_elements) != len(new_elements)
        or not old_elements
        or not all(
            isinstance(element, dict) for element in old_elements + new_elements
        )
        or [element.get("type") for element in old_elements]
        != [element.get("type") for element in new_elements]
    ):
        return None

    source_by_signature = defaultdict(deque)
    source_signatures = Counter()
    candidate_signatures = Counter()
    candidate_segments = []
    for element in old_elements:
        if element.get("type") != "text":
            continue
        for signature, source in _atomic_text_segments(element.get("content")):
            if not signature:
                return None
            source_by_signature[signature].append(source)
            source_signatures[signature] += 1
    for element in new_elements:
        segments = (
            _atomic_text_segments(element.get("content"))
            if element.get("type") == "text"
            else []
        )
        if any(not signature for signature, _ in segments):
            return None
        candidate_segments.append(segments)
        candidate_signatures.update(signature for signature, _ in segments)
    if not source_signatures:
        return None
    signature_aliases = {}
    if source_signatures != candidate_signatures:
        missing = list((source_signatures - candidate_signatures).elements())
        added = list((candidate_signatures - source_signatures).elements())
        if len(missing) != 1 or len(added) != 1:
            return None
        source_signature, candidate_signature = missing[0], added[0]
        matching_positions = sum(
            source_token == candidate_token
            for source_token, candidate_token in zip(
                source_signature, candidate_signature
            )
        )
        if (
            len(source_signature) != len(candidate_signature)
            or len(source_signature) < 3
            or matching_positions != len(source_signature) - 1
            or SequenceMatcher(
                None,
                "".join(source_signature),
                "".join(candidate_signature),
                autojunk=False,
            ).ratio()
            < 0.85
        ):
            return None
        signature_aliases[candidate_signature] = source_signature

    canonical = copy.deepcopy(candidate)
    segment_index = 0
    for element in canonical["elements"]:
        segments = candidate_segments[segment_index]
        segment_index += 1
        if element.get("type") != "text":
            continue
        exact = []
        for signature, _ in segments:
            source_signature = signature_aliases.get(signature, signature)
            if not source_by_signature[source_signature]:
                return None
            exact.append(source_by_signature[source_signature].popleft())
        element["content"] = "\n".join(exact)
    if any(values for values in source_by_signature.values()):
        return None
    return canonical if _reassignment_only(original, canonical) else None


def _merge_reassignment(original: dict, candidate: dict) -> dict | None:
    """Apply validated text moves while retaining parser metadata and evidence."""
    if not _reassignment_only(original, candidate):
        candidate = _canonicalized_reassignment(original, candidate)
        if candidate is None:
            return None
    merged = copy.deepcopy(original)
    for old, new in zip(merged["elements"], candidate["elements"]):
        if old.get("type") == "text":
            old["content"] = new.get("content")
    return merged


def _changed_text_indexes(original: dict, candidate: dict) -> list[int]:
    old_elements = original.get("elements") or []
    new_elements = candidate.get("elements") or []
    if len(old_elements) != len(new_elements):
        return []
    return [
        index
        for index, (old, new) in enumerate(zip(old_elements, new_elements))
        if isinstance(old, dict)
        and isinstance(new, dict)
        and old.get("type") == new.get("type") == "text"
        and old.get("content") != new.get("content")
    ]


def _normalized_words(value: object) -> str:
    return " ".join(
        re.findall(
            r"\w+",
            unicodedata.normalize("NFKC", str(value or "")).casefold(),
            flags=re.UNICODE,
        )
    )


def _final_review_accepts(
    verdict: object, candidate: dict, changed_indexes: list[int]
) -> bool:
    """Accept only a complete, image-grounded mapping of every changed panel."""
    if (
        not isinstance(verdict, dict)
        or verdict.get("pass") is not True
        or verdict.get("reading_order_matches") is not True
        or not changed_indexes
    ):
        return False
    mappings = verdict.get("changed_elements")
    if not isinstance(mappings, list) or len(mappings) != len(changed_indexes):
        return False

    elements = candidate.get("elements") or []
    expected = set(changed_indexes)
    observed = set()
    positions = {}
    for mapping in mappings:
        if not isinstance(mapping, dict):
            return False
        index = mapping.get("candidate_index")
        if type(index) is not int or index not in expected or index in observed:
            return False
        if (
            mapping.get("content_matches_visible_card") is not True
            or type(mapping.get("image_row")) is not int
            or mapping["image_row"] < 1
            or type(mapping.get("image_column")) is not int
            or mapping["image_column"] < 1
            or not str(mapping.get("evidence") or "").strip()
        ):
            return False
        if index >= len(elements) or not isinstance(elements[index], dict):
            return False
        segments = _atomic_text_segments(elements[index].get("content"))
        title = _normalized_words(mapping.get("visible_card_title"))
        candidate_title = _normalized_words(segments[0][1] if segments else "")
        if not title or title != candidate_title:
            return False
        observed.add(index)
        position = (mapping["image_row"], mapping["image_column"])
        if position in positions.values():
            return False
        positions[index] = position
    return observed == expected and [
        positions[index] for index in sorted(observed)
    ] == sorted(positions.values())


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
                LAYOUT_CONSISTENCY_REPAIR_MAX_TOKENS,
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
        repaired = None
        segment_spec = _card_segment_spec(original)
        if segment_spec is not None:
            try:
                assignment = _response_json(
                    client,
                    model,
                    image_path,
                    SEGMENT_ASSIGN_SYSTEM,
                    _segment_assignment_user_prompt(segment_spec),
                    LAYOUT_CONSISTENCY_REPAIR_MAX_TOKENS,
                )
            except Exception as exc:
                record["segment_assignment_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )
            else:
                repaired = _apply_card_segment_assignment(
                    original, segment_spec, assignment
                )
                if repaired is not None:
                    record["repair_mode"] = "immutable_segment_assignment"

        if repaired is None:
            repair_user = _repair_user_prompt(public, raw_text)
            try:
                generated = _response_json(
                    client,
                    model,
                    image_path,
                    REPAIR_SYSTEM,
                    repair_user,
                    LAYOUT_CONSISTENCY_REPAIR_MAX_TOKENS,
                )
            except Exception as exc:
                record["error"] = (
                    f"repair request failed: {type(exc).__name__}: {exc}"
                )
                results.append(record)
                continue
            if not isinstance(generated, dict):
                record["error"] = "repair returned invalid JSON"
                results.append(record)
                continue
            repaired = _merge_reassignment(original, generated)
            if repaired is not None:
                record["repair_mode"] = "full_text_reassignment"
        if repaired is None:
            record["error"] = "repair was not a text-only inventory-preserving reassignment"
            results.append(record)
            continue
        changed_indexes = _changed_text_indexes(original, repaired)
        if not changed_indexes:
            record["error"] = "repair did not change any text assignment"
            results.append(record)
            continue
        final_public = json.dumps(_public_candidate(repaired), ensure_ascii=False)
        try:
            final_verdict = _response_json(
                client,
                model,
                image_path,
                FINAL_VERIFY_SYSTEM,
                (
                    "Changed text element indexes:\n"
                    f"<changed_indexes>{json.dumps(changed_indexes)}</changed_indexes>\n\n"
                    "Structured candidate:\n"
                    f"<candidate>{final_public}</candidate>"
                ),
                LAYOUT_CONSISTENCY_REPAIR_MAX_TOKENS,
            )
        except Exception as exc:
            record["error"] = f"final review failed: {type(exc).__name__}: {exc}"
            results.append(record)
            continue
        record["final_review"] = final_verdict
        if not _final_review_accepts(final_verdict, repaired, changed_indexes):
            record["error"] = "repaired candidate failed grounded visual re-review"
            results.append(record)
            continue
        repaired["layout_consistency_repair"] = True
        persist_page(stem, json.dumps(repaired, ensure_ascii=False))
        record["accepted"] = True
        results.append(record)
    return results
