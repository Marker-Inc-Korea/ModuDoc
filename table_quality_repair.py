"""Targeted VLM repair for pages with objectively low-quality table structure."""

from __future__ import annotations

import base64
import argparse
import copy
import io
import json
import logging
import os
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from html import escape
from pathlib import Path

from bs4 import BeautifulSoup

import table_validate


logger = logging.getLogger(__name__)

TABLE_QUALITY_REPAIR = os.environ.get("TABLE_QUALITY_REPAIR", "1") == "1"
TABLE_QUALITY_REPAIR_MAX_PAGES = max(0, int(os.environ.get("TABLE_QUALITY_REPAIR_MAX_PAGES", "8")))
TABLE_QUALITY_REPAIR_TABLE_ATTEMPTS = min(
    3, max(1, int(os.environ.get("TABLE_QUALITY_REPAIR_TABLE_ATTEMPTS", "3")))
)
TABLE_QUALITY_REPAIR_MAX_TOKENS = max(1024, int(os.environ.get("TABLE_QUALITY_REPAIR_MAX_TOKENS", "16384")))
TABLE_QUALITY_REPAIR_TIMEOUT = max(60, int(os.environ.get("TABLE_QUALITY_REPAIR_TIMEOUT", "600")))
TABLE_QUALITY_REPAIR_IMG_MAXW = max(1024, int(os.environ.get("TABLE_QUALITY_REPAIR_IMG_MAXW", "2464")))
TABLE_QUALITY_REPAIR_TRIM_WHITESPACE = (
    os.environ.get("TABLE_QUALITY_REPAIR_TRIM_WHITESPACE", "1") == "1"
)
TABLE_QUALITY_REPAIR_CROPS = (
    os.environ.get("TABLE_QUALITY_REPAIR_CROPS", "1") == "1"
)
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
REVIEW_ISSUES = {
    "possible_cross_row_bleed",
    "possible_internal_duplicate_text",
    "possible_nested_layout_mismatch",
}

SYSTEM_PROMPT = """You are repairing a document page whose previous table extraction failed structural QA.
Use the PAGE IMAGE as the sole source of truth. Return the COMPLETE page, not only the broken table.
Output ONLY one valid JSON object:
{"page_number": int, "elements": [{"type": "heading_1|heading_2|heading_3|toc_entry|text|table|figure|footnote", "content": "...", "caption": "", "description": ""}]}

Rules:
- Preserve every visible heading, body block, key-value banner, table, note, and figure in reading order.
- Transcribe only visible content. Do not infer, summarize, translate, or silently correct wording.
- If the previous table was a false positive and the image shows only an equation,
  formula, or borderless variable-definition list, emit text elements instead.
- A table is HTML using <table>, <tr>, <td>, <br>, colspan, and rowspan. Do not use <th>.
- One continuous bordered grid is one table. Side-by-side grids are separate tables. Top/bottom grids with a separate border or gap and different column structures are separate tables.
- Preserve two visually repeated tables even when their cells contain identical text.
- Keep a genuinely nested grid inside its parent <td> only when it is visibly inside that outer cell.
- Trace the OUTER vertical borders and column borders before any inner boxes.
  An outer-row boundary meets those outer column borders; it may stop beside a
  genuine rowspan. A short line whose two endpoints remain strictly inside one
  parent cell belongs to an inner box or nested grid and must not create extra
  outer <tr> rows. Keep text above and below it in the same parent <td>.
- Every table must expand to a rectangle after colspan/rowspan. Do not add an all-empty padding column.
- Preserve blank cells bounded by visible grid lines, including trailing blank header cells; do not absorb them into a neighboring colspan.
- Preserve every visually merged header and body cell with the corresponding
  colspan or rowspan. Never shift values between columns or omit trailing cells.
- Return valid JSON without markdown fences or commentary."""


TABLE_ONLY_SYSTEM_PROMPT = """You repair malformed table geometry in document images.
Return ONLY one valid JSON object using this recursive table tree:
{"page_number": int, "tables": [{"caption": "", "rows": [{"cells": [{"text": "plain visible text", "colspan": 1, "rowspan": 1, "nested_table": {"rows": [{"cells": [{"text": "plain inner text"}]}]}}]}]}]}

Rules:
- Locate only the grid(s) corresponding to the supplied broken table reference.
- Return only corrected tables, not headings or surrounding page prose.
- The rows directly under each item in `tables` are OUTER rows only. Put every
  inner-box row under the containing cell's `nested_table`; never append an
  inner row to the outer `rows` list.
- Use plain text with `\n` for visible line breaks. Do not write HTML tags. Omit
  `nested_table` when a cell has no inner grid. Omit spans when they equal 1.
- Use the image as truth; the supplied cell strings are a text-preservation reference, not a geometry reference.
- The supplied expanded row widths and most-frequent width are diagnostics from
  the old HTML. Use them to locate malformed colspan/rowspan, but let visible
  borders in the image override the hint.
- Preserve all visible cells and their reading order. Do not summarize or silently correct wording.
- A supplied diagnostic may indicate that the next row's label was appended to
  the preceding row's final cell. Recheck the visible row borders and keep each
  label and value inside its own cell; never copy a row label across a boundary.
- Describe geometry only with `rows`, `cells`, `colspan`, `rowspan`, and
  `nested_table`; the parser will generate the HTML tags.
- Count the visible top-level cell regions in each row before assigning spans, then expand every row mentally and ensure it resolves to the same column count.
- Keep every blank cell bounded by visible grid lines, including leading and trailing blank header cells. Never absorb such a cell into a neighboring colspan.
- Preserve merged headers and genuinely nested grids with correct colspan/rowspan.
- Trace the OUTER vertical borders and column borders first. An outer-row
  boundary meets those borders; it may stop beside a genuine rowspan. A short
  line whose two endpoints remain strictly inside one parent cell is an inner-
  box or nested-grid boundary and must not create another outer row. Keep all
  surrounding text and the `nested_table` in that same parent cell.
- A full-width section band is its own row with colspan covering every outer column;
  never attach it to one data column or split the data row below it.
- Separate visually separate grids. Never add an all-empty padding column.
- If the supplied reference is not a real bordered grid in the image, return an empty `tables` array.
- Output no markdown fences or commentary."""


NESTED_LAYOUT_REVIEW_SYSTEM = """You independently verify repaired table geometry against a page image.
Return ONLY one valid JSON object:
{"pass": true|false, "outer_columns": int, "outer_rows": int, "nested_cells": [{"outer_row": int, "outer_column": int}], "reason": "short geometric reason"}

Judge geometry only, not punctuation, bullets, whitespace, spelling, or prose style.
- Trace the outer vertical borders and column borders before looking at inner boxes.
- An outer-row boundary meets outer column borders and may stop beside a rowspan;
  a horizontal line whose endpoints both remain inside one outer cell is not an outer-row boundary.
- A nested grid must remain inside the correct parent cell; its rows must not shift neighboring outer-column content into extra outer rows.
- Every outer section label and its adjacent descriptions must occupy the same outer row as in the image.
- Set pass=true only when outer column count, outer row sequence, merged cells, and nested parent-cell placement all match the image."""


def _norm(text: object) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(char for char in value if char.isalnum())


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
        metadata_issues = set(element.get("_issues") or [])
        allow_nested = (
            element.get("_source") == "native_table"
            or "nested_table_kept" in metadata_issues
        )
        quality = table_validate.assess_table_quality(
            element.get("content"), element.get("caption"), allow_nested=allow_nested
        )
        quality_issues = set(quality.get("issues") or [])
        issues = quality_issues & (HARD_ISSUES | REVIEW_ISSUES)
        if (
            "nested_table_kept" in metadata_issues
            and element.get("_source") not in (None, "native_table")
        ):
            issues.add("possible_nested_layout_mismatch")
        if issues or float(quality.get("confidence") or 0.0) < 0.75:
            problems.append({"index": index, "issues": sorted(issues), "quality": quality})
    return problems


def _preview_tables(data: dict) -> dict:
    preview = {**data, "elements": []}
    for element in data.get("elements") or []:
        if not isinstance(element, dict) or element.get("type") != "table" or not element.get("content"):
            preview["elements"].append(element)
            continue
        repaired, _, repair_issues = table_validate.validate_and_repair_table(
            element.get("content"), element.get("caption")
        )
        for item in repaired:
            candidate = {**element, "content": item.get("content")}
            if item.get("caption") is not None:
                candidate["caption"] = item.get("caption")
            if "nested_table_kept" in set(repair_issues or []):
                existing = list(candidate.get("_issues") or [])
                if "nested_table_kept" not in existing:
                    existing.append("nested_table_kept")
                candidate["_issues"] = existing
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


def _table_chunks(element: dict) -> set[str]:
    values = []
    soup = BeautifulSoup(element.get("content") or "", "html.parser")
    values.extend(cell.get_text(" ", strip=True) for cell in soup.find_all(["td", "th"]))
    if element.get("caption"):
        values.append(str(element.get("caption")))

    chunks = set()
    for value in values:
        normalized = _norm(value)
        if len(normalized) >= 2:
            chunks.add(normalized)
        chunks.update(
            token.casefold()
            for token in re.findall(r"[^\W_]{2,}", value, flags=re.UNICODE)
        )
    return chunks


def _table_visible_text(element: dict) -> str:
    soup = BeautifulSoup(element.get("content") or "", "html.parser")
    value = soup.get_text(" ", strip=True)
    if element.get("caption"):
        value += " " + str(element.get("caption"))
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        char for char in value
        if char.isalnum() or char in "%+-~□☐☑✓"
    )


def _page_visible_text(data: dict) -> str:
    values = []
    for element in data.get("elements") or []:
        if not isinstance(element, dict):
            continue
        if element.get("type") == "table":
            content = BeautifulSoup(
                element.get("content") or "", "html.parser"
            ).get_text(" ", strip=True)
        else:
            content = str(element.get("content") or "")
        values.extend(
            [content, str(element.get("caption") or ""), str(element.get("description") or "")]
        )
    value = unicodedata.normalize("NFKC", " ".join(values)).casefold()
    return "".join(char for char in value if char.isalnum())


def _counter_overlap(left: str, right: str) -> int:
    return sum((Counter(left) & Counter(right)).values())


def _table_match_metrics(original: dict, candidate: dict) -> dict:
    old_text = _table_visible_text(original)
    new_text = _table_visible_text(candidate)
    shared_chars = _counter_overlap(old_text, new_text)
    old_chunks = _table_chunks(original)
    new_chunks = _table_chunks(candidate)
    shared_chunks = old_chunks & new_chunks
    old_caption = _norm(original.get("caption"))
    new_caption = _norm(candidate.get("caption"))
    caption_similarity = (
        SequenceMatcher(None, old_caption, new_caption).ratio()
        if old_caption and new_caption else 0.0
    )
    allow_nested = "nested_table_kept" in set(candidate.get("_issues") or [])
    quality = table_validate.assess_table_quality(
        candidate.get("content"), candidate.get("caption"), allow_nested=allow_nested
    )
    hard_issues = sorted(set(quality.get("issues") or []) & HARD_ISSUES)
    char_recall = shared_chars / max(1, len(old_text))
    char_precision = shared_chars / max(1, len(new_text))
    chunk_recall = len(shared_chunks) / max(1, len(old_chunks))
    chunk_precision = len(shared_chunks) / max(1, len(new_chunks))
    sequence_similarity = SequenceMatcher(
        None, old_text, new_text, autojunk=False
    ).ratio()
    score = (
        0.30 * char_recall
        + 0.20 * char_precision
        + 0.20 * chunk_recall
        + 0.10 * chunk_precision
        + 0.10 * caption_similarity
        + 0.10 * sequence_similarity
    )
    eligible = bool(
        old_text
        and new_text
        and not hard_issues
        and float(quality.get("confidence") or 0.0) >= 0.75
        and char_recall >= 0.90
        and char_precision >= 0.80
        and (chunk_recall >= 0.50 or caption_similarity >= 0.75)
    )
    return {
        "eligible": eligible,
        "score": round(score, 4),
        "char_recall": round(char_recall, 4),
        "char_precision": round(char_precision, 4),
        "chunk_recall": round(chunk_recall, 4),
        "chunk_precision": round(chunk_precision, 4),
        "caption_similarity": round(caption_similarity, 4),
        "sequence_similarity": round(sequence_similarity, 4),
        "candidate_quality": quality,
    }


def graft_improved_tables(
    original: dict,
    candidate: dict,
    min_sequence_similarity: float = 0.98,
) -> tuple[dict, list[dict]]:
    """Replace only strongly matched broken tables, preserving the rest of the page."""
    candidate = _preview_tables(candidate)
    original_elements = list(original.get("elements") or [])
    candidate_elements = list(candidate.get("elements") or [])
    problem_indices = {item["index"] for item in _problem_tables(original)}
    candidate_indices = [
        index for index, element in enumerate(candidate_elements)
        if isinstance(element, dict) and element.get("type") == "table"
    ]

    matches = []
    for old_index in sorted(problem_indices):
        old_element = original_elements[old_index]
        for new_index in candidate_indices:
            metrics = _table_match_metrics(old_element, candidate_elements[new_index])
            if (
                metrics["eligible"]
                and metrics["sequence_similarity"] >= min_sequence_similarity
            ):
                matches.append((metrics["score"], old_index, new_index, metrics))

    rebuilt = [dict(item) if isinstance(item, dict) else item for item in original_elements]
    used_old = set()
    used_new = set()
    grafts = []
    for _, old_index, new_index, metrics in sorted(matches, reverse=True):
        if old_index in used_old or new_index in used_new:
            continue
        old_element = original_elements[old_index]
        new_element = candidate_elements[new_index]
        replacement = {
            key: value for key, value in old_element.items() if not key.startswith("_")
        }
        replacement["type"] = "table"
        replacement["content"] = new_element.get("content")
        for key in ("caption", "description"):
            if new_element.get(key) not in (None, ""):
                replacement[key] = new_element.get(key)
        rebuilt[old_index] = replacement
        used_old.add(old_index)
        used_new.add(new_index)
        grafts.append(
            {"original_index": old_index, "candidate_index": new_index, **metrics}
        )

    result = {**original, "elements": rebuilt}
    if grafts:
        result["quality_repair"] = True
    return result, grafts


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


def _problem_table_sequence_preserved(
    original: dict,
    candidate: dict,
    minimum_similarity: float,
) -> bool:
    original_elements = list(original.get("elements") or [])
    candidate_tables = [
        element
        for element in candidate.get("elements") or []
        if isinstance(element, dict) and element.get("type") == "table"
    ]
    match_options = []
    for problem in _problem_tables(original):
        old_index = problem["index"]
        if not (0 <= old_index < len(original_elements)):
            return False
        old_table = original_elements[old_index]
        options = [
            candidate_index
            for candidate_index, new_table in enumerate(candidate_tables)
            if (
                (metrics := _table_match_metrics(old_table, new_table))["eligible"]
                and metrics["sequence_similarity"] >= minimum_similarity
            )
        ]
        if not options:
            return False
        match_options.append(options)

    # A single repaired table cannot account for multiple source tables, even
    # when those source tables happen to contain the same visible strings.
    assigned = set()

    def match(problem_index: int) -> bool:
        if problem_index == len(match_options):
            return True
        for candidate_index in match_options[problem_index]:
            if candidate_index in assigned:
                continue
            assigned.add(candidate_index)
            if match(problem_index + 1):
                return True
            assigned.remove(candidate_index)
        return False

    match_options.sort(key=len)
    return match(0)


def candidate_improvement(
    original: dict,
    candidate: dict,
    min_table_sequence_similarity: float = 0.98,
) -> tuple[bool, dict]:
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
    order_similarity = SequenceMatcher(
        None, _page_visible_text(original), _page_visible_text(candidate)
    ).ratio()
    table_sequence_preserved = _problem_table_sequence_preserved(
        original, candidate, min_table_sequence_similarity
    )
    nested_layout_topology_changed = _nested_layout_topology_changed(
        original, candidate
    )
    metrics = {
        "old_problem_tables": len(old_problems),
        "new_problem_tables": len(new_problems),
        "old_tables": old_tables,
        "new_tables": new_tables,
        "coverage": round(coverage, 4),
        "char_ratio": round(char_ratio, 4),
        "old_orphan_headings": old_orphans,
        "new_orphan_headings": new_orphans,
        "order_similarity": round(order_similarity, 4),
        "table_sequence_preserved": table_sequence_preserved,
        "nested_layout_topology_changed": nested_layout_topology_changed,
    }
    accepted = bool(
        old_problems
        and len(new_problems) < len(old_problems)
        and new_tables >= old_tables
        and coverage >= TABLE_QUALITY_REPAIR_MIN_COVERAGE
        and char_ratio >= TABLE_QUALITY_REPAIR_MIN_COVERAGE
        and order_similarity >= 0.80
        and table_sequence_preserved
        and nested_layout_topology_changed
        and (old_orphans == 0 or new_orphans < old_orphans)
    )
    return accepted, metrics


def _crop_content_bbox(image):
    """Trim large blank page margins while retaining conservative padding."""
    if not TABLE_QUALITY_REPAIR_TRIM_WHITESPACE:
        return image
    grayscale = image.convert("L")
    mask = grayscale.point(lambda value: 255 if value < 248 else 0)
    bbox = mask.getbbox()
    if not bbox:
        return image
    left, top, right, bottom = bbox
    width, height = image.size
    pad_x = max(8, int(width * 0.02))
    pad_y = max(8, int(height * 0.02))
    expanded = (
        max(0, left - pad_x),
        max(0, top - pad_y),
        min(width, right + pad_x),
        min(height, bottom + pad_y),
    )
    cropped_width = expanded[2] - expanded[0]
    cropped_height = expanded[3] - expanded[1]
    if cropped_width >= width * 0.96 and cropped_height >= height * 0.96:
        return image
    return image.crop(expanded)


def _encode_pil_image(image) -> str:
    from PIL import Image

    image = image.convert("RGB")
    if image.width > TABLE_QUALITY_REPAIR_IMG_MAXW:
        ratio = TABLE_QUALITY_REPAIR_IMG_MAXW / image.width
        image = image.resize(
            (TABLE_QUALITY_REPAIR_IMG_MAXW, max(1, int(image.height * ratio))),
            Image.LANCZOS,
        )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _encode_image(path: Path) -> str:
    from PIL import Image

    with Image.open(path) as source:
        image = _crop_content_bbox(source.convert("RGB"))
        return _encode_pil_image(image)


def _encode_table_images(path: Path) -> list[str]:
    """Return a page overview plus enlarged overlapping vertical bands."""
    from PIL import Image

    with Image.open(path) as source:
        image = _crop_content_bbox(source.convert("RGB"))
        variants = [image.copy()]
        if TABLE_QUALITY_REPAIR_CROPS and image.height > image.width * 1.15:
            band_height = max(1, round(image.height * 0.62))
            variants.extend(
                [
                    image.crop((0, 0, image.width, band_height)),
                    image.crop(
                        (
                            0,
                            max(0, image.height - band_height),
                            image.width,
                            image.height,
                        )
                    ),
                ]
            )
    return [_encode_pil_image(variant) for variant in variants[:3]]


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


def _request_json(client, request: dict) -> dict | None:
    try:
        response = client.chat.completions.create(
            **request, response_format={"type": "json_object"}
        )
    except Exception:
        response = client.chat.completions.create(**request)
    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        return None
    return _first_json(choice.message.content or "")


def _render_table_tree(table_spec: dict, depth: int = 0) -> str | None:
    """Render a bounded recursive table tree into balanced, escaped HTML."""
    if not isinstance(table_spec, dict) or depth > 3:
        return None
    rows = table_spec.get("rows")
    if not isinstance(rows, list) or not rows or len(rows) > 200:
        return None

    rendered_rows = []
    for row in rows:
        cells = row.get("cells") if isinstance(row, dict) else None
        if not isinstance(cells, list) or not cells or len(cells) > 100:
            return None
        rendered_cells = []
        for cell in cells:
            if not isinstance(cell, dict):
                return None
            text = cell.get("text", "")
            if not isinstance(text, str) or len(text) > 100000:
                return None

            attributes = []
            for key in ("colspan", "rowspan"):
                value = cell.get(key, 1)
                if type(value) is not int or not (1 <= value <= 100):
                    return None
                if value != 1:
                    attributes.append(f' {key}="{value}"')

            content = escape(text).replace("\r\n", "\n").replace("\r", "\n")
            content = content.replace("\n", "<br>")
            nested_spec = cell.get("nested_table")
            if nested_spec is not None:
                nested = _render_table_tree(nested_spec, depth + 1)
                if nested is None:
                    return None
                if content:
                    content += "<br>"
                content += nested
            rendered_cells.append(
                f"<td{''.join(attributes)}>{content}</td>"
            )
        rendered_rows.append(f"<tr>{''.join(rendered_cells)}</tr>")
    return f"<table>{''.join(rendered_rows)}</table>"


def _table_tree_candidate(value: dict | None) -> dict | None:
    """Convert model table trees to the page candidate used by repair gates."""
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("elements"), list):
        return value
    tables = value.get("tables")
    if not isinstance(tables, list) or len(tables) > 20:
        return None

    elements = []
    for table_spec in tables:
        if not isinstance(table_spec, dict):
            return None
        content = _render_table_tree(table_spec)
        if content is None:
            return None
        element = {"type": "table", "content": content}
        caption = table_spec.get("caption")
        if caption not in (None, ""):
            if not isinstance(caption, str):
                return None
            element["caption"] = caption
        elements.append(element)
    return {"page_number": value.get("page_number", 0), "elements": elements}


def _table_geometry(element: dict) -> dict:
    try:
        table = BeautifulSoup(
            element.get("content") or "", "html.parser"
        ).find("table")
        if table is None:
            return {}
        _, widths, row_count, max_width = table_validate._build_grid(
            table_validate._rows_of(table)
        )
        if not widths:
            return {}
        most_frequent_width = max(
            Counter(widths).items(), key=lambda item: (item[1], item[0])
        )[0]
        return {
            "expanded_row_widths": widths,
            "most_frequent_width": most_frequent_width,
            "most_frequent_width_count": widths.count(most_frequent_width),
            "row_count": row_count,
            "maximum_width": max_width,
        }
    except Exception:
        return {}


def _nested_review_geometry_consistent(candidate: dict, review: dict) -> bool:
    """Require a visual verdict to agree with the parsed outer HTML grid."""
    try:
        reviewed_rows = int(review.get("outer_rows"))
        reviewed_columns = int(review.get("outer_columns"))
    except (TypeError, ValueError):
        return False
    if reviewed_rows < 1 or reviewed_columns < 1:
        return False

    for element in candidate.get("elements") or []:
        if not isinstance(element, dict) or element.get("type") != "table":
            continue
        geometry = _table_geometry(element)
        widths = geometry.get("expanded_row_widths") or []
        if (
            geometry.get("row_count") == reviewed_rows
            and len(widths) == reviewed_rows
            and all(width == reviewed_columns for width in widths)
        ):
            return True
    return False


def _nested_review_geometry_feedback(candidate: dict, review: dict) -> str:
    reviewed_rows = review.get("outer_rows")
    reviewed_columns = review.get("outer_columns")
    parsed = [
        _table_geometry(element)
        for element in candidate.get("elements") or []
        if isinstance(element, dict) and element.get("type") == "table"
    ]
    parsed_summary = [
        {
            "rows": geometry.get("row_count"),
            "expanded_row_widths": geometry.get("expanded_row_widths") or [],
        }
        for geometry in parsed
    ]
    return (
        "The visual reviewer counted a rectangular outer grid of "
        f"{reviewed_rows} rows by {reviewed_columns} columns, but an HTML parser "
        f"reads the candidate outer table geometry as {parsed_summary}. "
        "Move inner-grid rows under their parent cell's nested_table instead of "
        "the outer rows list, and return a rectangular outer table whose parsed "
        "row and column counts match the image."
    )


def _table_topology_signature(element: dict) -> tuple:
    """Describe row/cell/span/nesting geometry without using document text."""
    try:
        root = BeautifulSoup(
            element.get("content") or "", "html.parser"
        ).find("table")
        if root is None:
            return ()

        def table_signature(table):
            return tuple(
                tuple(
                    (
                        table_validate._span_int(cell, "colspan"),
                        table_validate._span_int(cell, "rowspan"),
                        tuple(
                            table_signature(nested)
                            for nested in cell.find_all("table", recursive=False)
                        ),
                    )
                    for cell in row.find_all(["td", "th"], recursive=False)
                )
                for row in table_validate._rows_of(table)
            )

        return table_signature(root)
    except Exception:
        return ()


def _nested_layout_topology_changed(original: dict, candidate: dict) -> bool:
    nested_problem_indices = [
        problem["index"]
        for problem in _problem_tables(original)
        if "possible_nested_layout_mismatch" in set(problem.get("issues") or [])
    ]
    if not nested_problem_indices:
        return True
    original_elements = original.get("elements") or []
    old_signatures = [
        _table_topology_signature(original_elements[index])
        for index in nested_problem_indices
        if 0 <= index < len(original_elements)
    ]
    new_signatures = {
        signature
        for element in candidate.get("elements") or []
        if isinstance(element, dict) and element.get("type") == "table"
        if (signature := _table_topology_signature(element))
    }
    return bool(old_signatures) and all(
        signature and signature not in new_signatures
        for signature in old_signatures
    )


def _table_text_reference(element: dict) -> dict:
    """Expose visible cell text without leaking the malformed HTML geometry."""
    reference = {}
    if element.get("caption") not in (None, ""):
        reference["caption"] = element.get("caption")
    try:
        table = BeautifulSoup(
            element.get("content") or "", "html.parser"
        ).find("table")
        if table is None:
            return reference
        reference["visible_rows"] = [
            [
                cell.get_text(" ", strip=True)
                for cell in row.find_all(["td", "th"], recursive=False)
            ]
            for row in table_validate._rows_of(table)
        ]
    except Exception:
        pass
    return reference


def _borderless_definition_text(element: dict) -> str | None:
    """Return lossless text for a small formula-variable definition layout."""
    try:
        table = BeautifulSoup(
            element.get("content") or "", "html.parser"
        ).find("table")
        if table is None or table.find("table") is not None:
            return None
        rows = table_validate._rows_of(table)
        if not (2 <= len(rows) <= 8):
            return None
        row_values = [
            [
                cell.get_text(" ", strip=True)
                for cell in row.find_all(["td", "th"], recursive=False)
            ]
            for row in rows
        ]
        if any(not values for values in row_values):
            return None
        colon_rows = sum(
            any(":" in value or "：" in value for value in values)
            for values in row_values
        )
        has_formula_notation = bool(
            table.find(["sub", "sup"])
            or re.search(r"[=∑Σ∏√±×÷]", table.get_text(" ", strip=True))
        )
        if colon_rows < max(2, len(rows) - 1) or not has_formula_notation:
            return None
        lines = [" ".join(value for value in values if value).strip() for values in row_values]
        if any(not line for line in lines):
            return None
        return "\n".join(lines)
    except Exception:
        return None


def _demote_borderless_definition_tables(
    image_path: Path, original: dict
) -> tuple[dict | None, list[dict]]:
    """Demote formula definitions only when the page image has no table grid."""
    from PIL import Image

    try:
        with Image.open(image_path) as source:
            image = source.convert("L")
    except Exception:
        return None, []

    candidate = copy.deepcopy(original)
    elements = candidate.get("elements") or []
    demotions = []
    for index, element in enumerate(elements):
        if not isinstance(element, dict) or element.get("type") != "table":
            continue
        text = _borderless_definition_text(element)
        if not text:
            continue
        geometry = _table_geometry(element)
        row_count = geometry.get("row_count") or 0
        if (
            row_count < 2
            or _table_horizontal_window(image, row_count)
            or len(_full_height_vertical_grid_lines(image)) >= 2
        ):
            continue
        old_cells = _table_visible_text({**element, "caption": ""})
        new_text = unicodedata.normalize("NFKC", text).casefold()
        new_text = "".join(
            char for char in new_text if char.isalnum() or char in "%+-~□☐☑✓"
        )
        if old_cells != new_text:
            continue
        replacement = {
            key: value for key, value in element.items() if not key.startswith("_")
        }
        replacement.update(
            {
                "type": "text",
                "content": text,
                "_source": "borderless_definition_text",
                "_confidence": 0.80,
                "_issues": ["borderless_definition_demoted"],
            }
        )
        elements[index] = replacement
        demotions.append({"index": index, "rows": row_count})
    return (candidate, demotions) if demotions else (None, [])


def _validated_borderless_definition_demotion(
    image_path: Path, original: dict
) -> tuple[dict | None, list[dict], dict | None]:
    """Return a deterministic, page-lossless demotion and its audit metrics."""
    demoted, demotions = _demote_borderless_definition_tables(image_path, original)
    if demoted is None:
        return None, [], None
    demoted["quality_repair"] = True
    visible_text_preserved = _page_visible_text(original) == _page_visible_text(demoted)
    old_problem_count = len(_problem_tables(original))
    new_problem_count = len(_problem_tables(demoted))
    old_table_count = sum(
        isinstance(element, dict) and element.get("type") == "table"
        for element in original.get("elements") or []
    )
    new_table_count = sum(
        isinstance(element, dict) and element.get("type") == "table"
        for element in demoted.get("elements") or []
    )
    if (
        not visible_text_preserved
        or new_problem_count > old_problem_count
        or old_table_count - new_table_count != len(demotions)
    ):
        return None, [], None
    metrics = {
        "old_problem_tables": old_problem_count,
        "new_problem_tables": new_problem_count,
        "visible_text_preserved": True,
        "demoted_tables": len(demotions),
        "old_table_count": old_table_count,
        "new_table_count": new_table_count,
    }
    return demoted, demotions, metrics


def _geometry_correction_choices(element: dict) -> dict:
    """Enumerate lossless row layouts that can fix majority-width outliers."""
    geometry = _table_geometry(element)
    widths = geometry.get("expanded_row_widths") or []
    target_width = geometry.get("most_frequent_width")
    if (
        not target_width
        or len(widths) < 3
        or widths.count(target_width) * 2 <= len(widths)
        or all(width == target_width for width in widths)
    ):
        return {}
    try:
        table = BeautifulSoup(
            element.get("content") or "", "html.parser"
        ).find("table")
        if table is None:
            return {}
        rows = table_validate._rows_of(table)
        if any(
            int(cell.get("rowspan", 1)) != 1
            for row in rows
            for cell in row.find_all(["td", "th"], recursive=False)
        ):
            return {}
        malformed_rows = []
        for row_index, (row, width) in enumerate(zip(rows, widths)):
            if width == target_width:
                continue
            delta = target_width - width
            choices = []
            cells = row.find_all(["td", "th"], recursive=False)
            if any(cell.find("table") is not None for cell in cells):
                return {}
            current_spans = [int(cell.get("colspan", 1)) for cell in cells]
            if delta < 0:
                for cell_index, cell in enumerate(cells):
                    current = int(cell.get("colspan", 1))
                    if (
                        current != -delta
                        or cell.get_text(" ", strip=True)
                        or cell.find("table") is not None
                    ):
                        continue
                    result_cells = [
                        {
                            "source_cell_index": source_index,
                            "cell_text": source_cell.get_text(" ", strip=True),
                            "colspan": int(source_cell.get("colspan", 1)),
                        }
                        for source_index, source_cell in enumerate(cells)
                        if source_index != cell_index
                    ]
                    choices.append(
                        {
                            "choice_id": f"r{row_index}c{cell_index}_dropempty",
                            "removed_empty_cell_indexes": [cell_index],
                            "result_cells": result_cells,
                        }
                    )
            for cell_index, cell in enumerate(cells):
                current = int(cell.get("colspan", 1))
                proposed = current + delta
                if not (1 <= proposed <= 100):
                    continue
                base_spans = list(current_spans)
                base_spans[cell_index] = proposed

                def add_choice(split_index: int | None = None, side: str = ""):
                    result_spans = list(base_spans)
                    if split_index is not None:
                        if result_spans[split_index] < 2:
                            return
                        result_spans[split_index] -= 1
                    result_cells = []
                    for source_index, (source_cell, span) in enumerate(
                        zip(cells, result_spans)
                    ):
                        if split_index == source_index and side == "before":
                            result_cells.append(
                                {"inserted_empty": True, "colspan": 1}
                            )
                        result_cells.append(
                            {
                                "source_cell_index": source_index,
                                "cell_text": source_cell.get_text(" ", strip=True),
                                "colspan": span,
                            }
                        )
                        if split_index == source_index and side == "after":
                            result_cells.append(
                                {"inserted_empty": True, "colspan": 1}
                            )
                    suffix = (
                        ""
                        if split_index is None
                        else f"_split{split_index}{side[0]}"
                    )
                    choices.append(
                        {
                            "choice_id": (
                                f"r{row_index}c{cell_index}_colspan{proposed}{suffix}"
                            ),
                            "result_cells": result_cells,
                        }
                    )

                add_choice()
                for split_index, span in enumerate(base_spans):
                    if span < 2:
                        continue
                    add_choice(split_index, "before")
                    add_choice(split_index, "after")
            if not choices:
                return {}
            malformed_rows.append(
                {
                    "row_index": row_index,
                    "current_width": width,
                    "required_width": target_width,
                    "choices": choices,
                }
            )
        return {
            "target_width": target_width,
            "malformed_rows": malformed_rows,
        }
    except Exception:
        return {}


def _longest_dark_run(values, threshold: int = 220) -> tuple[int, int, int]:
    best_start = best_end = current_start = 0
    in_run = False
    for index, value in enumerate(values):
        if value < threshold:
            if not in_run:
                current_start = index
                in_run = True
            if index + 1 - current_start > best_end - best_start:
                best_start, best_end = current_start, index + 1
        else:
            in_run = False
    return best_end - best_start, best_start, best_end


def _group_adjacent_lines(lines: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    groups = []
    for line in lines:
        if not groups or line[0] > groups[-1][-1][0] + 1:
            groups.append([])
        groups[-1].append(line)
    return [max(group, key=lambda item: item[3]) for group in groups]


def _horizontal_grid_lines(image) -> list[tuple[int, int, int, int]]:
    width, height = image.size
    data = image.tobytes()
    minimum = max(120, int(width * 0.25))
    lines = []
    for y in range(height):
        length, start, end = _longest_dark_run(
            data[y * width : (y + 1) * width], threshold=160
        )
        if length >= minimum:
            lines.append((y, start, end, length))
    return _group_adjacent_lines(lines)


def _full_height_vertical_grid_lines(image) -> list[tuple[int, int, int, int]]:
    """Find long vertical borders without assuming horizontal grid lines."""
    width, height = image.size
    data = image.tobytes()
    minimum = max(50, int(height * 0.08))
    lines = []
    for x in range(width):
        length, start, end = _longest_dark_run(
            data[x::width], threshold=160
        )
        if length >= minimum:
            lines.append((x, start, end, length))
    return _group_adjacent_lines(lines)


def _table_horizontal_window(image, row_count: int) -> list[tuple[int, int, int, int]]:
    lines = _horizontal_grid_lines(image)
    needed = row_count + 1
    if len(lines) < needed:
        return []
    tolerance = max(8, int(image.width * 0.012))
    candidates = []
    for offset in range(len(lines) - needed + 1):
        window = lines[offset : offset + needed]
        starts = sorted(line[1] for line in window)
        ends = sorted(line[2] for line in window)
        left = starts[len(starts) // 2]
        right = ends[len(ends) // 2]
        if right - left < max(120, int(image.width * 0.25)):
            continue
        if any(
            abs(line[1] - left) > tolerance
            or abs(line[2] - right) > tolerance
            for line in window
        ):
            continue
        if any(b[0] - a[0] < 5 for a, b in zip(window, window[1:])):
            continue
        score = sum(line[3] for line in window)
        candidates.append((score, window))
    if not candidates:
        return []
    candidates.sort(key=lambda item: item[0], reverse=True)
    if len(candidates) > 1:
        return []
    return candidates[0][1]


def _vertical_grid_lines(image, left: int, right: int, top: int, bottom: int) -> list[int]:
    top += 3
    bottom -= 3
    if bottom - top < 6:
        return []
    required = max(4, int((bottom - top) * 0.72))
    lines = []
    pixels = image.load()
    for x in range(max(0, left - 4), min(image.width, right + 5)):
        values = [pixels[x, y] for y in range(top, bottom)]
        length, _, _ = _longest_dark_run(values)
        if length >= required:
            lines.append((x, 0, 0, length))
    return [line[0] for line in _group_adjacent_lines(lines)]


def _grid_line_geometry_correction(image_path: Path, candidate: dict) -> dict | None:
    """Resolve table-row spans only when raster grid lines provide unique evidence."""
    from PIL import Image

    eligible = []
    for table_index, element in enumerate(
        element
        for element in candidate.get("elements") or []
        if isinstance(element, dict) and element.get("type") == "table"
    ):
        choices = _geometry_correction_choices(element)
        if choices:
            eligible.append((table_index, element, choices))
    if len(eligible) != 1:
        return None
    table_index, element, allowed = eligible[0]
    geometry = _table_geometry(element)
    row_count = geometry.get("row_count") or 0
    target_width = allowed["target_width"]
    try:
        with Image.open(image_path) as source:
            image = source.convert("L")
            horizontal = _table_horizontal_window(image, row_count)
            if not horizontal:
                return None
            left = round(sum(line[1] for line in horizontal) / len(horizontal))
            right = round(sum(line[2] for line in horizontal) / len(horizontal))
            row_lines = [
                _vertical_grid_lines(image, left, right, upper[0], lower[0])
                for upper, lower in zip(horizontal, horizontal[1:])
            ]
    except Exception:
        return None

    base_rows = [lines for lines in row_lines if len(lines) == target_width + 1]
    if len(base_rows) < 2:
        return None
    position_tolerance = max(6, int((right - left) * 0.012))
    base_boundaries = [
        round(sum(lines[index] for lines in base_rows) / len(base_rows))
        for index in range(target_width + 1)
    ]
    if any(
        abs(lines[index] - base_boundaries[index]) > position_tolerance
        for lines in base_rows
        for index in range(target_width + 1)
    ):
        return None

    selected = []
    for malformed_row in allowed["malformed_rows"]:
        row_index = malformed_row["row_index"]
        if not (0 <= row_index < len(row_lines)):
            return None
        mapped = []
        for boundary in row_lines[row_index]:
            nearest = min(
                range(len(base_boundaries)),
                key=lambda index: abs(base_boundaries[index] - boundary),
            )
            if abs(base_boundaries[nearest] - boundary) > position_tolerance:
                return None
            if not mapped or mapped[-1] != nearest:
                mapped.append(nearest)
        if not mapped or mapped[0] != 0 or mapped[-1] != target_width:
            return None
        observed_spans = [b - a for a, b in zip(mapped, mapped[1:])]
        matching = [
            choice["choice_id"]
            for choice in malformed_row["choices"]
            if [cell["colspan"] for cell in choice["result_cells"]]
            == observed_spans
        ]
        if len(matching) != 1:
            return None
        selected.append(matching[0])
    return {"tables": [{"table_index": table_index, "choice_ids": selected}]}


def _request_table_candidate(
    client,
    model: str,
    image_path: Path,
    original: dict,
    problems: list[dict],
    reviewer_feedback: str = "",
) -> dict | None:
    references = []
    elements = original.get("elements") or []
    for problem in problems:
        index = problem.get("index")
        if not isinstance(index, int) or not (0 <= index < len(elements)):
            continue
        element = elements[index]
        if not isinstance(element, dict):
            continue
        references.append(
            {
                "index": index,
                "issues": problem.get("issues") or [],
                "geometry_hint": _table_geometry(element),
                "table_text_reference": _table_text_reference(element),
            }
        )
    if not references:
        return None
    user_text = (
        "Broken table references:\n"
        f"<broken_tables>{json.dumps(references, ensure_ascii=False)}</broken_tables>\n\n"
        "The images are the same page: first an overview, then enlarged overlapping vertical bands when present. "
        "Return only the corresponding corrected table grid(s)."
    )
    if reviewer_feedback:
        user_text += (
            "\n\nAn independent layout reviewer rejected the previous attempt:\n"
            f"<review_feedback>{reviewer_feedback[:2000]}</review_feedback>\n"
            "Rebuild the geometry from the image and do not repeat that layout error."
        )
    images = _encode_table_images(image_path)
    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": TABLE_ONLY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}"},
                    }
                    for encoded in images
                ] + [{"type": "text", "text": user_text}],
            },
        ],
        "temperature": 0,
        "max_tokens": TABLE_QUALITY_REPAIR_MAX_TOKENS,
        "timeout": TABLE_QUALITY_REPAIR_TIMEOUT,
        "extra_body": {"repetition_penalty": 1.08, "no_repeat_ngram_size": 24},
    }
    try:
        return _table_tree_candidate(_request_json(client, request))
    except Exception:
        if len(images) <= 1:
            raise
        request["messages"][1]["content"] = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{images[0]}"},
            },
            {"type": "text", "text": user_text},
        ]
        return _table_tree_candidate(_request_json(client, request))


def _needs_nested_layout_review(problems: list[dict]) -> bool:
    return any(
        "possible_nested_layout_mismatch" in set(problem.get("issues") or [])
        for problem in problems
    )


def _request_nested_layout_review(
    client,
    model: str,
    image_path: Path,
    candidate: dict,
) -> dict | None:
    images = _encode_table_images(image_path)
    user_text = (
        "Repaired table candidate:\n"
        f"<candidate>{_public_candidate(candidate)}</candidate>\n\n"
        "The images are the same page: first an overview, then enlarged overlapping vertical bands when present. "
        "Independently verify the outer grid and every nested grid's parent cell."
    )
    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": NESTED_LAYOUT_REVIEW_SYSTEM},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}"},
                    }
                    for encoded in images
                ]
                + [{"type": "text", "text": user_text}],
            },
        ],
        "temperature": 0,
        "max_tokens": TABLE_QUALITY_REPAIR_MAX_TOKENS,
        "timeout": TABLE_QUALITY_REPAIR_TIMEOUT,
        "extra_body": {"repetition_penalty": 1.08, "no_repeat_ngram_size": 24},
    }
    try:
        return _request_json(client, request)
    except Exception:
        if len(images) <= 1:
            raise
        request["messages"][1]["content"] = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{images[0]}"},
            },
            {"type": "text", "text": user_text},
        ]
        return _request_json(client, request)


def _apply_geometry_correction(candidate: dict, correction: dict | None) -> dict | None:
    """Apply a grid-line-selected span edit without regenerating table text."""
    if not isinstance(correction, dict) or not isinstance(correction.get("tables"), list):
        return None
    corrected = copy.deepcopy(candidate)
    table_elements = [
        element
        for element in corrected.get("elements") or []
        if isinstance(element, dict) and element.get("type") == "table"
    ]
    changed = False
    for table_patch in correction["tables"]:
        if not isinstance(table_patch, dict):
            continue
        table_index = table_patch.get("table_index")
        choice_ids = table_patch.get("choice_ids")
        if (
            type(table_index) is not int
            or not (0 <= table_index < len(table_elements))
            or not isinstance(choice_ids, list)
            or not choice_ids
        ):
            continue
        element = table_elements[table_index]
        allowed = _geometry_correction_choices(element)
        target_width = allowed.get("target_width")
        malformed_rows = allowed.get("malformed_rows") or []
        choices_by_id = {
            choice["choice_id"]: {**choice, "row_index": row["row_index"]}
            for row in malformed_rows
            for choice in row["choices"]
        }
        if (
            not target_width
            or len(choice_ids) != len(malformed_rows)
            or any(type(choice_id) is not str for choice_id in choice_ids)
            or any(choice_id not in choices_by_id for choice_id in choice_ids)
            or len({choices_by_id[choice_id]["row_index"] for choice_id in choice_ids})
            != len(malformed_rows)
        ):
            continue

        soup = BeautifulSoup(element.get("content") or "", "html.parser")
        table = soup.find("table")
        if table is None:
            continue
        rows = table_validate._rows_of(table)
        for choice_id in choice_ids:
            choice = choices_by_id[choice_id]
            row_index = choice["row_index"]
            row = rows[row_index]
            cells = row.find_all(["td", "th"], recursive=False)
            result_cells = choice["result_cells"]
            source_indexes = [
                cell_spec["source_cell_index"]
                for cell_spec in result_cells
                if "source_cell_index" in cell_spec
            ]
            removed_indexes = choice.get("removed_empty_cell_indexes") or []
            if (
                any(type(index) is not int or not (0 <= index < len(cells)) for index in removed_indexes)
                or any(cells[index].get_text(" ", strip=True) for index in removed_indexes)
                or source_indexes
                != [index for index in range(len(cells)) if index not in removed_indexes]
            ):
                return None
            rebuilt_cells = []
            for cell_spec in result_cells:
                if cell_spec.get("inserted_empty"):
                    rebuilt = soup.new_tag("td")
                else:
                    rebuilt = copy.copy(cells[cell_spec["source_cell_index"]])
                colspan = cell_spec["colspan"]
                if colspan == 1:
                    rebuilt.attrs.pop("colspan", None)
                else:
                    rebuilt["colspan"] = str(colspan)
                rebuilt_cells.append(rebuilt)
            row.clear()
            row.extend(rebuilt_cells)

        patched_element = {**element, "content": str(table)}
        after_geometry = _table_geometry(patched_element)
        after_widths = after_geometry.get("expanded_row_widths") or []
        if not after_widths or any(width != target_width for width in after_widths):
            continue
        element["content"] = patched_element["content"]
        changed = True
    return corrected if changed else None


def _short_alpha_header(text: object, maximum: int) -> bool:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    letters = [char for char in value if char.isalpha()]
    return (
        2 <= len(value) <= maximum
        and len(letters) >= 2
        and all(char.isalpha() or char in " '-" for char in value)
        and not value.isupper()
    )


def _cell_has_contrasting_ink(image, left: int, right: int, top: int, bottom: int) -> bool:
    margin_x = max(3, min(8, (right - left) // 12))
    margin_y = max(3, min(8, (bottom - top) // 12))
    if right - left <= margin_x * 2 or bottom - top <= margin_y * 2:
        return False
    values = image.crop(
        (left + margin_x, top + margin_y, right - margin_x, bottom - margin_y)
    ).tobytes()
    if not values:
        return False
    ordered = sorted(values)
    background = ordered[len(ordered) // 2]
    contrasting = sum(abs(value - background) >= 40 for value in values)
    fraction = contrasting / len(values)
    return contrasting >= max(16, int(len(values) * 0.0015)) and fraction < 0.40


def _internal_horizontal_segments(
    image, left: int, right: int, top: int, bottom: int
) -> list[tuple[int, int, int, int]]:
    """Find substantial short lines contained within one outer cell."""
    left = max(0, left)
    right = min(image.width, right)
    top = max(0, top + 5)
    bottom = min(image.height, bottom - 5)
    if right - left < 20 or bottom - top < 20:
        return []
    pixels = image.load()
    minimum = max(24, int((right - left) * 0.35))
    lines = []
    for y in range(top, bottom):
        values = [pixels[x, y] for x in range(left, right)]
        length, start, end = _longest_dark_run(values, threshold=160)
        if length >= minimum:
            lines.append((y, left + start, left + end, length))
    return _group_adjacent_lines(lines)


def _raster_outer_grid_shape(image, row_count: int, column_count: int) -> dict | None:
    """Find one long-line outer grid with one full-width section row."""
    if row_count < 3 or column_count < 2:
        return None
    lines = _horizontal_grid_lines(image)
    if len(lines) < row_count + 1:
        return None

    endpoint_tolerance = max(8, int(image.width * 0.012))
    endpoint_groups = []
    for line in lines:
        matches = []
        for group_index, group in enumerate(endpoint_groups):
            left = round(sum(item[1] for item in group) / len(group))
            right = round(sum(item[2] for item in group) / len(group))
            if (
                abs(line[1] - left) <= endpoint_tolerance
                and abs(line[2] - right) <= endpoint_tolerance
            ):
                matches.append(
                    (abs(line[1] - left) + abs(line[2] - right), group_index)
                )
        if matches:
            endpoint_groups[min(matches)[1]].append(line)
        else:
            endpoint_groups.append([line])

    candidates = []
    duplicate_tolerance = max(4, int(image.height * 0.003))
    for group in endpoint_groups:
        deduplicated = []
        for line in sorted(group):
            if (
                deduplicated
                and line[0] - deduplicated[-1][0] <= duplicate_tolerance
            ):
                if line[3] > deduplicated[-1][3]:
                    deduplicated[-1] = line
                continue
            deduplicated.append(line)
        if len(deduplicated) != row_count + 1:
            continue
        left = round(sum(item[1] for item in deduplicated) / len(deduplicated))
        right = round(sum(item[2] for item in deduplicated) / len(deduplicated))
        if right - left < max(120, int(image.width * 0.35)):
            continue
        boundaries = [item[0] for item in deduplicated]
        if any(lower - upper < 12 for upper, lower in zip(boundaries, boundaries[1:])):
            continue

        row_lines = []
        for upper, lower in zip(boundaries, boundaries[1:]):
            detected = _vertical_grid_lines(image, left, right, upper, lower)
            row_lines.append(
                [
                    value
                    for value in detected
                    if abs(value - left) > endpoint_tolerance
                    and abs(value - right) > endpoint_tolerance
                ]
            )
        if any(len(items) not in (0, column_count - 1) for items in row_lines):
            continue
        section_rows = [
            index for index, items in enumerate(row_lines) if not items
        ]
        full_rows = [items for items in row_lines if items]
        if len(section_rows) != 1 or len(full_rows) != row_count - 1:
            continue
        columns = [
            round(sum(items[index] for items in full_rows) / len(full_rows))
            for index in range(column_count - 1)
        ]
        if any(
            abs(items[index] - columns[index]) > endpoint_tolerance
            for items in full_rows
            for index in range(column_count - 1)
        ):
            continue
        candidates.append(
            {
                "rows": row_count,
                "columns": column_count,
                "section_row": section_rows[0],
                "horizontal_boundaries": boundaries,
                "vertical_boundaries": [left, *columns, right],
            }
        )
    return candidates[0] if len(candidates) == 1 else None


def _direct_cell_placements(rows) -> list[list[dict]]:
    """Map each direct cell to its logical grid start and source span."""
    carry = {}
    placements = []
    for row_index, row in enumerate(rows):
        occupied = {column for column, remaining in carry.items() if remaining > 0}
        additions = {}
        row_placements = []
        column = 0
        for cell in row.find_all(["td", "th"], recursive=False):
            while column in occupied:
                column += 1
            colspan = table_validate._span_int(cell, "colspan")
            rowspan = table_validate._span_int(cell, "rowspan")
            covered = range(column, column + colspan)
            if any(item in occupied for item in covered):
                return []
            row_placements.append(
                {
                    "row": row_index,
                    "column": column,
                    "colspan": colspan,
                    "rowspan": rowspan,
                    "cell": cell,
                }
            )
            for item in covered:
                occupied.add(item)
                if rowspan > 1:
                    additions[item] = max(additions.get(item, 0), rowspan - 1)
            column += colspan
        carry = {
            item: remaining - 1
            for item, remaining in carry.items()
            if remaining - 1 > 0
        }
        for item, remaining in additions.items():
            carry[item] = max(carry.get(item, 0), remaining)
        placements.append(row_placements)
    return placements


def _cell_own_normalized_text(cell) -> str:
    fragment = BeautifulSoup(str(cell), "html.parser")
    for nested in fragment.find_all("table"):
        nested.decompose()
    return re.sub(r"\s+", "", fragment.get_text(" ", strip=True))


def _raster_evidenced_rowspan_section_rebuild(
    image_path: Path, data: dict
) -> tuple[dict | None, list[dict]]:
    """Lift a misplaced section band and collapse its rowspan detail group."""
    from PIL import Image

    try:
        with Image.open(image_path) as source:
            image = source.convert("L")
    except Exception:
        return None, []

    candidates = []
    for element_index, element in enumerate(data.get("elements") or []):
        if (
            not isinstance(element, dict)
            or element.get("type") != "table"
            or "nested_table_kept" not in set(element.get("_issues") or [])
        ):
            continue
        soup = BeautifulSoup(element.get("content") or "", "html.parser")
        table = soup.find("table")
        if table is None or table.find("table") is None:
            continue
        rows = table_validate._rows_of(table)
        geometry = _table_geometry(element)
        widths = geometry.get("expanded_row_widths") or []
        column_count = geometry.get("most_frequent_width") or 0
        if (
            len(rows) < 5
            or column_count < 2
            or len(widths) != len(rows)
            or any(width != column_count for width in widths)
        ):
            continue
        placements = _direct_cell_placements(rows)
        if len(placements) != len(rows):
            continue

        for group_start, row_placements in enumerate(placements):
            anchors = [
                item
                for item in row_placements
                if item["colspan"] == 1 and 3 <= item["rowspan"] <= 6
            ]
            if len(anchors) != 1:
                continue
            anchor = anchors[0]
            group_size = anchor["rowspan"]
            group_end = group_start + group_size
            if group_end > len(rows):
                continue
            group = [
                item
                for row_items in placements[group_start:group_end]
                for item in row_items
            ]
            if (
                any(item["colspan"] != 1 for item in group)
                or any(item["row"] + item["rowspan"] > group_end for item in group)
            ):
                continue

            band_options = []
            for item in row_placements:
                text = _cell_own_normalized_text(item["cell"])
                if (
                    item is anchor
                    or item["rowspan"] != 1
                    or item["cell"].find("table") is not None
                    or not (2 <= len(text) <= 80)
                ):
                    continue
                band_options.append((len(text), item))
            band_options.sort(key=lambda value: value[0])
            if not band_options or (
                len(band_options) > 1
                and band_options[0][0] * 2 > band_options[1][0]
            ):
                continue
            band = band_options[0][1]

            coverage = Counter()
            for item in group:
                if item is band:
                    continue
                for row_index in range(item["row"], item["row"] + item["rowspan"]):
                    coverage[(row_index, item["column"])] += 1
            if any(
                coverage[(row_index, column)] != (
                    0
                    if row_index == group_start and column == band["column"]
                    else 1
                )
                for row_index in range(group_start, group_end)
                for column in range(column_count)
            ):
                continue

            output_row_count = len(rows) - group_size + 2
            raster = _raster_outer_grid_shape(
                image, output_row_count, column_count
            )
            if not raster or raster["section_row"] != group_start:
                continue

            rebuilt_table = soup.new_tag("table")
            for row in rows[:group_start]:
                rebuilt_table.append(copy.copy(row))

            section_row = soup.new_tag("tr")
            section_cell = copy.copy(band["cell"])
            section_cell.attrs.pop("rowspan", None)
            section_cell["colspan"] = str(column_count)
            section_row.append(section_cell)
            rebuilt_table.append(section_row)

            merged_row = soup.new_tag("tr")
            valid = True
            merged_top = raster["horizontal_boundaries"][group_start + 1]
            merged_bottom = raster["horizontal_boundaries"][group_start + 2]
            for column in range(column_count):
                parts = sorted(
                    [
                        item
                        for item in group
                        if item is not band and item["column"] == column
                    ],
                    key=lambda item: item["row"],
                )
                if not parts:
                    valid = False
                    break
                if len(parts) > 1 and not _internal_horizontal_segments(
                    image,
                    raster["vertical_boundaries"][column],
                    raster["vertical_boundaries"][column + 1],
                    merged_top,
                    merged_bottom,
                ):
                    valid = False
                    break
                if len(parts) == 1:
                    merged_cell = copy.copy(parts[0]["cell"])
                    merged_cell.attrs.pop("rowspan", None)
                    merged_cell.attrs.pop("colspan", None)
                    merged_row.append(merged_cell)
                    continue

                plain = [
                    item for item in parts if item["cell"].find("table") is None
                ]
                if not plain:
                    valid = False
                    break
                ranked_plain = sorted(
                    plain,
                    key=lambda item: (item["rowspan"], item["row"]),
                    reverse=True,
                )
                if (
                    len(ranked_plain) > 1
                    and ranked_plain[0]["rowspan"] == ranked_plain[1]["rowspan"]
                ):
                    valid = False
                    break
                base = ranked_plain[0]
                merged_cell = soup.new_tag("td")
                for part_index, part in enumerate(parts):
                    if part_index:
                        merged_cell.append(soup.new_tag("br"))
                    source_cell = part["cell"]
                    if part is base or source_cell.find("table") is not None:
                        for child in list(source_cell.contents):
                            merged_cell.append(copy.copy(child))
                        continue
                    nested_table = soup.new_tag("table")
                    nested_row = soup.new_tag("tr")
                    nested_cell = soup.new_tag("td")
                    for child in list(source_cell.contents):
                        nested_cell.append(copy.copy(child))
                    nested_row.append(nested_cell)
                    nested_table.append(nested_row)
                    merged_cell.append(nested_table)
                merged_row.append(merged_cell)
            if not valid:
                continue
            rebuilt_table.append(merged_row)
            for row in rows[group_end:]:
                rebuilt_table.append(copy.copy(row))

            candidate = copy.deepcopy(data)
            patched_element = candidate["elements"][element_index]
            patched_element["content"] = str(rebuilt_table)
            for key in ("_source", "_confidence", "_issues", "_native"):
                patched_element.pop(key, None)
            candidate_geometry = _table_geometry(patched_element)
            if candidate_geometry.get("expanded_row_widths") != (
                [column_count] * output_row_count
            ):
                continue
            candidates.append(
                (
                    candidate,
                    {
                        "element_index": element_index,
                        "strategy": "raster_rowspan_section_group_rebuilt",
                        "source_rows": len(rows),
                        "output_rows": output_row_count,
                        "columns": column_count,
                        "group_size": group_size,
                    },
                )
            )

    if len(candidates) != 1:
        return None, []
    candidate, change = candidates[0]
    if Counter(_page_visible_text(data)) != Counter(_page_visible_text(candidate)):
        return None, []
    return candidate, [change]


def _raster_evidenced_header_rebuild(
    image_path: Path, data: dict
) -> tuple[dict | None, list[dict]]:
    """Rebuild a malformed two-row header only when raster cells prove its layout."""
    from PIL import Image

    candidates = []
    table_number = -1
    for element_index, element in enumerate(data.get("elements") or []):
        if not isinstance(element, dict) or element.get("type") != "table":
            continue
        table_number += 1
        soup = BeautifulSoup(element.get("content") or "", "html.parser")
        table = soup.find("table")
        if table is None or table.find("table") is not None:
            continue
        rows = table_validate._rows_of(table)
        geometry = _table_geometry(element)
        widths = geometry.get("expanded_row_widths") or []
        target_width = geometry.get("most_frequent_width") or 0
        if (
            len(rows) < 4
            or target_width < 4
            or len(widths) != len(rows)
            or widths.count(target_width) * 2 <= len(widths)
            or len(set(widths)) < 2
        ):
            continue
        first_cells = rows[0].find_all(["td", "th"], recursive=False)
        second_cells = rows[1].find_all(["td", "th"], recursive=False)
        missing = target_width - len(second_cells)
        if (
            not (1 <= missing <= 3)
            or any(
                int(cell.get("colspan", 1) or 1) != 1
                or int(cell.get("rowspan", 1) or 1) != 1
                for cell in second_cells
            )
            or any(
                int(cell.get("rowspan", 1) or 1) != 1
                for row in rows[2:]
                for cell in row.find_all(["td", "th"], recursive=False)
            )
        ):
            continue

        starts = []
        column = 0
        valid_spans = True
        for cell in first_cells:
            try:
                colspan = max(1, int(cell.get("colspan", 1) or 1))
                rowspan = max(1, int(cell.get("rowspan", 1) or 1))
            except (TypeError, ValueError):
                valid_spans = False
                break
            starts.append((column, colspan, rowspan, cell))
            column += colspan
        if not valid_spans:
            continue
        movers = starts[:missing]
        if (
            [start for start, _, _, _ in movers] != list(range(missing))
            or any(
                colspan != 1
                or rowspan != 2
                or not cell.get_text(" ", strip=True)
                for _, colspan, rowspan, cell in movers
            )
        ):
            continue
        remainder = starts[missing:]
        continuations = [
            item
            for item in remainder
            if item[1] == 1
            and item[2] == 2
            and _short_alpha_header(item[3].get_text(" ", strip=True), 24)
        ]
        if len(continuations) != 1:
            continue
        continuation = continuations[0]
        group_cells = [
            item
            for item in remainder
            if item is not continuation and item[3].get_text(" ", strip=True)
        ]
        if not group_cells:
            continue

        try:
            with Image.open(image_path) as source:
                image = source.convert("L")
                horizontal = _table_horizontal_window(image, len(rows))
                if not horizontal:
                    continue
                left = round(sum(line[1] for line in horizontal) / len(horizontal))
                right = round(sum(line[2] for line in horizontal) / len(horizontal))
                row_lines = [
                    _vertical_grid_lines(image, left, right, upper[0], lower[0])
                    for upper, lower in zip(horizontal, horizontal[1:])
                ]
                base_rows = [
                    lines for lines in row_lines if len(lines) == target_width + 1
                ]
                if len(base_rows) < 2:
                    continue
                tolerance = max(6, int((right - left) * 0.012))
                boundaries = [
                    round(sum(lines[index] for lines in base_rows) / len(base_rows))
                    for index in range(target_width + 1)
                ]
                if any(
                    abs(lines[index] - boundaries[index]) > tolerance
                    for lines in base_rows
                    for index in range(target_width + 1)
                ):
                    continue
                mapped = []
                for boundary in row_lines[0]:
                    nearest = min(
                        range(len(boundaries)),
                        key=lambda index: abs(boundaries[index] - boundary),
                    )
                    if abs(boundaries[nearest] - boundary) > tolerance:
                        mapped = []
                        break
                    if not mapped or mapped[-1] != nearest:
                        mapped.append(nearest)
                if not mapped or mapped[0] != 0 or mapped[-1] != target_width:
                    continue
                observed = list(zip(mapped, mapped[1:]))
                if any(end <= start for start, end in observed):
                    continue
                top, separator, bottom = (
                    horizontal[0][0],
                    horizontal[1][0],
                    horizontal[2][0],
                )
                occupied = [
                    _cell_has_contrasting_ink(
                        image,
                        boundaries[start],
                        boundaries[end],
                        top,
                        separator,
                    )
                    for start, end in observed
                ]
                if sum(occupied) != len(group_cells):
                    continue

                def top_occupied_at(column_index):
                    return any(
                        present and start <= column_index < end
                        for (start, end), present in zip(observed, occupied)
                    )

                moved_columns = [start for start, _, _, _ in movers]
                continuation_column = continuation[0]
                lower_target = continuation_column - 1
                if (
                    any(top_occupied_at(column_index) for column_index in moved_columns)
                    or top_occupied_at(continuation_column)
                    or not (0 <= lower_target < target_width)
                    or any(
                        not _cell_has_contrasting_ink(
                            image,
                            boundaries[column_index],
                            boundaries[column_index + 1],
                            separator,
                            bottom,
                        )
                        for column_index in moved_columns + [lower_target]
                    )
                ):
                    continue
        except Exception:
            continue

        rebuilt_second = [copy.copy(item[3]) for item in movers] + [
            copy.copy(cell) for cell in second_cells
        ]
        if len(rebuilt_second) != target_width:
            continue
        target_cell = rebuilt_second[lower_target]
        if not _short_alpha_header(target_cell.get_text(" ", strip=True), 48):
            continue
        for cell in rebuilt_second:
            cell.attrs.pop("colspan", None)
            cell.attrs.pop("rowspan", None)
        target_cell.append(soup.new_tag("br"))
        for child in list(continuation[3].contents):
            target_cell.append(copy.copy(child))

        rebuilt_first = []
        group_iterator = iter(group_cells)
        for (start, end), present in zip(observed, occupied):
            if present:
                rebuilt = copy.copy(next(group_iterator)[3])
                rebuilt.attrs.pop("rowspan", None)
            else:
                rebuilt = soup.new_tag("td")
            span = end - start
            if span == 1:
                rebuilt.attrs.pop("colspan", None)
            else:
                rebuilt["colspan"] = str(span)
            rebuilt_first.append(rebuilt)

        rows[0].clear()
        rows[0].extend(rebuilt_first)
        rows[1].clear()
        rows[1].extend(rebuilt_second)
        patched = copy.deepcopy(data)
        patched_element = patched["elements"][element_index]
        patched_element["content"] = str(table)
        for key in ("_source", "_confidence", "_issues", "_native"):
            patched_element.pop(key, None)
        if any(
            width != target_width
            for width in _table_geometry(patched_element).get(
                "expanded_row_widths", []
            )
        ):
            continue
        candidates.append(
            (
                patched,
                {
                    "element_index": element_index,
                    "table_index": table_number,
                    "strategy": "raster_header_cells_rebuilt",
                },
            )
        )

    if len(candidates) != 1:
        return None, []
    candidate, change = candidates[0]
    return candidate, [change]


def _normalize_grouped_stub_rowspans(data: dict) -> tuple[dict | None, list[dict]]:
    """Restore repeated row groups whose left stub spans a fixed number of rows."""
    candidate = copy.deepcopy(data)
    changes = []
    for element_index, element in enumerate(data.get("elements") or []):
        if not isinstance(element, dict) or element.get("type") != "table":
            continue
        soup = BeautifulSoup(element.get("content") or "", "html.parser")
        table = soup.find("table")
        if table is None or table.find("table") is not None:
            continue
        rows = table_validate._rows_of(table)
        if len(rows) < 7:
            continue
        header = rows[0].find_all(["td", "th"], recursive=False)
        body = [row.find_all(["td", "th"], recursive=False) for row in rows[1:]]
        if (
            len(header) < 3
            or not all(cell.name == "th" for cell in header)
            or any(
                table_validate._span_int(cell, "colspan") != 1
                or table_validate._span_int(cell, "rowspan") != 1
                for row_cells in [header] + body
                for cell in row_cells
            )
        ):
            continue
        target_width = len(header) + 1
        starts = [
            index
            for index, cells in enumerate(body)
            if len(cells) == target_width
            and cells[0].find("br") is not None
            and cells[0].get_text(" ", strip=True)
            and cells[1].get_text(" ", strip=True)
        ]
        if len(starts) < 2:
            continue
        group_sizes = [right - left for left, right in zip(starts, starts[1:])]
        if not group_sizes or len(set(group_sizes)) != 1:
            continue
        group_size = group_sizes[0]
        groups_end = starts[-1] + group_size
        if (
            starts[0] != 0
            or group_size < 3
            or groups_end != len(body) - 1
        ):
            continue

        valid = True
        for start in starts:
            group = body[start:start + group_size]
            if len(group) != group_size or not any(
                len(cells) == target_width - 1 for cells in group[1:]
            ):
                valid = False
                break
            if any(len(cells) not in {target_width - 1, target_width} for cells in group[1:]):
                valid = False
                break
            for cells in group[1:]:
                if len(cells) == target_width and not any(
                    not cell.get_text(" ", strip=True) and cell.find("table") is None
                    for cell in cells[1:]
                ):
                    valid = False
                    break
            if not valid:
                break
        summary = body[-1]
        if (
            not valid
            or len(summary) != target_width
            or summary[0].find("br") is not None
            or not summary[0].get_text(" ", strip=True)
            or sum(not cell.get_text(" ", strip=True) for cell in summary[1:]) < 2
        ):
            continue

        def remove_redundant_empty(cells):
            for cell in cells[1:]:
                if not cell.get_text(" ", strip=True) and cell.find("table") is None:
                    cell.extract()
                    return True
            return False

        header[0]["colspan"] = "2"
        for start in starts:
            group = body[start:start + group_size]
            group[0][0]["rowspan"] = str(group_size)
            for cells in group[1:]:
                if len(cells) == target_width and not remove_redundant_empty(cells):
                    valid = False
                    break
            if not valid:
                break
        if not valid:
            continue
        summary[0]["colspan"] = "2"
        if not remove_redundant_empty(summary):
            continue
        patched_element = candidate["elements"][element_index]
        patched_element["content"] = str(table)
        for key in ("_source", "_confidence", "_issues", "_native"):
            patched_element.pop(key, None)
        widths = _table_geometry(patched_element).get("expanded_row_widths") or []
        if not widths or any(width != target_width for width in widths):
            continue
        changes.append(
            {
                "element_index": element_index,
                "strategy": "grouped_stub_rowspans_restored",
                "groups": len(starts),
                "group_size": group_size,
            }
        )
    return (candidate, changes) if changes else (None, [])


def _normalize_sparse_continuation_rows(data: dict) -> tuple[dict | None, list[dict]]:
    """Build a text-lossless geometry candidate for common VLM row artifacts."""
    candidate = copy.deepcopy(data)
    changes = []
    for element_index, element in enumerate(candidate.get("elements") or []):
        if not isinstance(element, dict) or element.get("type") != "table":
            continue
        soup = BeautifulSoup(element.get("content") or "", "html.parser")
        table = soup.find("table")
        if table is None or table.find("table") is not None:
            continue
        rows = table_validate._rows_of(table)
        merged_row = None
        for row_index in range(1, len(rows)):
            previous_cells = rows[row_index - 1].find_all(
                ["td", "th"], recursive=False
            )
            cells = rows[row_index].find_all(["td", "th"], recursive=False)
            nonempty = [
                index
                for index, cell in enumerate(cells)
                if cell.get_text(" ", strip=True)
            ]
            if (
                len(cells) < 4
                or len(cells) != len(previous_cells)
                or len(nonempty) != 1
                or sum(bool(cell.get_text(" ", strip=True)) for cell in previous_cells)
                < max(3, len(previous_cells) // 2)
                or any(int(cell.get("colspan", 1)) != 1 for cell in cells + previous_cells)
                or any(int(cell.get("rowspan", 1)) != 1 for cell in cells + previous_cells)
            ):
                continue
            cell_index = nonempty[0]
            continuation = cells[cell_index].get_text(" ", strip=True)
            previous_text = previous_cells[cell_index].get_text(" ", strip=True)
            if (
                not re.fullmatch(r"[A-Za-z][A-Za-z'-]{1,23}", continuation)
                or continuation.isupper()
                or not re.fullmatch(r"[A-Za-z][A-Za-z '-]{1,47}", previous_text)
            ):
                continue
            previous_cells[cell_index].append(soup.new_tag("br"))
            for child in list(cells[cell_index].contents):
                previous_cells[cell_index].append(copy.copy(child))
            rows[row_index].decompose()
            merged_row = row_index
            changes.append(
                {
                    "element_index": element_index,
                    "strategy": "sparse_continuation_row_merged",
                    "row_index": row_index,
                    "cell_index": cell_index,
                }
            )
            break

        rows = table_validate._rows_of(table)
        _, row_widths, _, _ = table_validate._build_grid(rows)
        modal_width = (
            Counter(row_widths).most_common(1)[0][0] if row_widths else 0
        )
        modal_rows = [
            row
            for row, width in zip(rows, row_widths)
            if width == modal_width
        ]
        trailing_cells = [
            row.find_all(["td", "th"], recursive=False)[-1]
            for row in modal_rows
            if row.find_all(["td", "th"], recursive=False)
        ]
        if (
            len(trailing_cells) == len(modal_rows)
            and len(modal_rows) >= 2
            and all(
                not cell.get_text(" ", strip=True)
                and int(cell.get("colspan", 1)) == 1
                and int(cell.get("rowspan", 1)) == 1
                and cell.find("table") is None
                for cell in trailing_cells
            )
        ):
            for cell in trailing_cells:
                cell.decompose()
            changes.append(
                {
                    "element_index": element_index,
                    "strategy": "uniform_empty_trailing_column_removed",
                }
            )

        if merged_row is not None or any(
            change["element_index"] == element_index for change in changes
        ):
            element["content"] = str(table)
            for key in ("_source", "_confidence", "_issues", "_native"):
                element.pop(key, None)
    return (candidate, changes) if changes else (None, [])


def _validated_deterministic_geometry_repair(
    image_path: Path, original: dict
) -> tuple[dict | None, list[dict], dict | None]:
    corrected, changes = _normalize_grouped_stub_rowspans(original)
    if corrected is None:
        corrected, changes = _raster_evidenced_rowspan_section_rebuild(
            image_path, original
        )
    if corrected is None:
        corrected, changes = _raster_evidenced_header_rebuild(image_path, original)
    if corrected is None:
        normalized, changes = _normalize_sparse_continuation_rows(original)
        if normalized is None:
            return None, [], None
        correction = _grid_line_geometry_correction(image_path, normalized)
        corrected = _apply_geometry_correction(normalized, correction)
        if corrected is None:
            return None, [], None
    old_problems = _problem_tables(original)
    new_problems = _problem_tables(corrected)
    old_table_count = sum(
        isinstance(element, dict) and element.get("type") == "table"
        for element in original.get("elements") or []
    )
    new_table_count = sum(
        isinstance(element, dict) and element.get("type") == "table"
        for element in corrected.get("elements") or []
    )
    text_inventory_preserved = Counter(_page_visible_text(original)) == Counter(
        _page_visible_text(corrected)
    )
    if (
        not old_problems
        or len(new_problems) >= len(old_problems)
        or old_table_count != new_table_count
        or not text_inventory_preserved
    ):
        return None, [], None
    corrected["quality_repair"] = True
    metrics = {
        "old_problem_tables": len(old_problems),
        "new_problem_tables": len(new_problems),
        "table_count": old_table_count,
        "text_inventory_preserved": True,
    }
    return corrected, changes, metrics


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
        "extra_body": {"repetition_penalty": 1.08, "no_repeat_ngram_size": 24},
    }
    return _request_json(client, request)


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
        has_formula_definition = any(
            isinstance(element, dict)
            and element.get("type") == "table"
            and _borderless_definition_text(element)
            for element in data.get("elements") or []
        )
        if problems or has_formula_definition:
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
        record["candidate_options"] = []
        deterministic, deterministic_changes, deterministic_metrics = (
            _validated_deterministic_geometry_repair(image_path, original)
        )
        if deterministic is not None:
            record["candidate_options"].append(
                {
                    "strategy": "raster_evidenced_geometry_repair",
                    "accepted": True,
                    "metrics": deterministic_metrics,
                    "changes": deterministic_changes,
                }
            )
            persist_page(stem, json.dumps(deterministic, ensure_ascii=False))
            record.update(
                {
                    "accepted": True,
                    "strategy": "raster_evidenced_geometry_repair",
                    "metrics": deterministic_metrics,
                    "changes": deterministic_changes,
                }
            )
            results.append(record)
            logger.info(
                "%s raster-evidenced geometry repair accepted: %s",
                stem,
                deterministic_metrics,
            )
            continue
        demoted, demotions, demotion_metrics = (
            _validated_borderless_definition_demotion(image_path, original)
        )
        if demoted is not None:
            record["candidate_options"].append(
                {
                    "strategy": "borderless_definition_demoted",
                    "accepted": True,
                    "metrics": demotion_metrics,
                    "demotions": demotions,
                }
            )
            persist_page(stem, json.dumps(demoted, ensure_ascii=False))
            record.update(
                {
                    "accepted": True,
                    "strategy": "borderless_definition_demoted",
                    "metrics": demotion_metrics,
                    "demotions": demotions,
                }
            )
            results.append(record)
            logger.info(
                "%s borderless definition demoted: %s",
                stem,
                demotion_metrics,
            )
            continue
        targeted_accepted = False
        review_only = bool(problems) and all(
            set(problem.get("issues") or []).issubset(REVIEW_ISSUES)
            for problem in problems
        )
        nested_layout_review = _needs_nested_layout_review(problems)
        reviewer_feedback = ""
        for attempt in range(1, TABLE_QUALITY_REPAIR_TABLE_ATTEMPTS + 1):
            try:
                table_candidate = _request_table_candidate(
                    client,
                    model,
                    image_path,
                    original,
                    problems,
                    reviewer_feedback=reviewer_feedback,
                )
            except Exception as exc:
                table_candidate = None
                record.setdefault("targeted_errors", []).append(
                    {"attempt": attempt, "error": f"{type(exc).__name__}: {exc}"}
                )
            if (
                not table_candidate
                or not isinstance(table_candidate.get("elements"), list)
                or not table_candidate.get("elements")
            ):
                record["candidate_options"].append(
                    {
                        "strategy": "targeted_table_graft",
                        "attempt": attempt,
                        "accepted": False,
                        "error": "repair returned invalid or empty JSON",
                        "grafts": [],
                    }
                )
                continue

            table_candidate["page_number"] = record["page"]
            layout_review_passed = False
            if nested_layout_review:
                try:
                    layout_review = _request_nested_layout_review(
                        client, model, image_path, table_candidate
                    )
                except Exception as exc:
                    layout_review = None
                    record.setdefault("layout_review_errors", []).append(
                        {"attempt": attempt, "error": f"{type(exc).__name__}: {exc}"}
                    )
                review_claimed_pass = bool(
                    isinstance(layout_review, dict)
                    and layout_review.get("pass") is True
                )
                review_geometry_consistent = bool(
                    review_claimed_pass
                    and _nested_review_geometry_consistent(
                        table_candidate, layout_review
                    )
                )
                layout_review_passed = bool(
                    review_claimed_pass and review_geometry_consistent
                )
                review_reason = (
                    str(layout_review.get("reason") or "")
                    if isinstance(layout_review, dict)
                    else "nested layout review returned no valid verdict"
                )
                if review_claimed_pass and not review_geometry_consistent:
                    review_reason = _nested_review_geometry_feedback(
                        table_candidate, layout_review
                    )
                record.setdefault("nested_layout_reviews", []).append(
                    {
                        "attempt": attempt,
                        "pass": layout_review_passed,
                        "review_claimed_pass": review_claimed_pass,
                        "candidate_geometry_consistent": review_geometry_consistent,
                        "outer_columns": (
                            layout_review.get("outer_columns")
                            if isinstance(layout_review, dict)
                            else None
                        ),
                        "outer_rows": (
                            layout_review.get("outer_rows")
                            if isinstance(layout_review, dict)
                            else None
                        ),
                        "reason": review_reason[:2000],
                    }
                )
                if not layout_review_passed:
                    reviewer_feedback = review_reason or (
                        "The outer grid or nested parent-cell placement did not match the image."
                    )
                    record["candidate_options"].append(
                        {
                            "strategy": "targeted_table_graft",
                            "attempt": attempt,
                            "accepted": False,
                            "error": "independent nested-layout review rejected candidate",
                            "grafts": [],
                        }
                    )
                    continue

            table_problems = _problem_tables(table_candidate)
            record.setdefault("targeted_diagnostics", []).append(
                {
                    "attempt": attempt,
                    "table_geometry": [
                        _table_geometry(element)
                        for element in table_candidate.get("elements") or []
                        if isinstance(element, dict)
                        and element.get("type") == "table"
                    ],
                    "problem_tables": len(table_problems),
                }
            )
            targeted_variants = [("targeted_table_graft", table_candidate)]
            if table_problems:
                try:
                    correction = _grid_line_geometry_correction(
                        image_path, table_candidate
                    )
                except Exception as exc:
                    correction = None
                    record.setdefault("geometry_correction_errors", []).append(
                        {"attempt": attempt, "error": f"{type(exc).__name__}: {exc}"}
                    )
                corrected = _apply_geometry_correction(table_candidate, correction)
                if corrected:
                    corrected["page_number"] = record["page"]
                    targeted_variants.append(("grid_corrected_table_graft", corrected))

            for targeted_strategy, targeted_candidate in targeted_variants:
                minimum_sequence = (
                    0.50
                    if layout_review_passed
                    else (
                        0.80
                        if targeted_strategy == "grid_corrected_table_graft"
                        or review_only
                        else 0.98
                    )
                )
                grafted, grafts = graft_improved_tables(
                    original,
                    targeted_candidate,
                    min_sequence_similarity=minimum_sequence,
                )
                if grafts:
                    accepted, metrics = candidate_improvement(
                        original,
                        grafted,
                        min_table_sequence_similarity=minimum_sequence,
                    )
                    record["candidate_options"].append(
                        {
                            "strategy": targeted_strategy,
                            "attempt": attempt,
                            "accepted": accepted,
                            "metrics": metrics,
                            "grafts": grafts,
                        }
                    )
                    if accepted:
                        persist_page(stem, json.dumps(grafted, ensure_ascii=False))
                        record.update(
                            {
                                "accepted": True,
                                "strategy": targeted_strategy,
                                "attempt": attempt,
                                "metrics": metrics,
                                "grafts": grafts,
                            }
                        )
                        results.append(record)
                        logger.info(
                            "%s %s accepted on attempt %d: %s",
                            stem,
                            targeted_strategy,
                            attempt,
                            metrics,
                        )
                        targeted_accepted = True
                        break
                else:
                    record["candidate_options"].append(
                        {
                            "strategy": targeted_strategy,
                            "attempt": attempt,
                            "accepted": False,
                            "error": "no strongly matched structurally sound table",
                            "grafts": [],
                        }
                    )
            if targeted_accepted:
                break
        if targeted_accepted:
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
        fallback_sequence = 0.98
        if nested_layout_review:
            try:
                fallback_review = _request_nested_layout_review(
                    client, model, image_path, candidate
                )
            except Exception as exc:
                fallback_review = None
                record.setdefault("layout_review_errors", []).append(
                    {"stage": "full_page", "error": f"{type(exc).__name__}: {exc}"}
                )
            fallback_review_claimed_pass = bool(
                isinstance(fallback_review, dict)
                and fallback_review.get("pass") is True
            )
            fallback_geometry_consistent = bool(
                fallback_review_claimed_pass
                and _nested_review_geometry_consistent(candidate, fallback_review)
            )
            fallback_review_passed = bool(
                fallback_review_claimed_pass and fallback_geometry_consistent
            )
            fallback_reason = (
                str(fallback_review.get("reason") or "")
                if isinstance(fallback_review, dict)
                else "nested layout review returned no valid verdict"
            )
            if fallback_review_claimed_pass and not fallback_geometry_consistent:
                fallback_reason = _nested_review_geometry_feedback(
                    candidate, fallback_review
                )
            record.setdefault("nested_layout_reviews", []).append(
                {
                    "stage": "full_page",
                    "pass": fallback_review_passed,
                    "review_claimed_pass": fallback_review_claimed_pass,
                    "candidate_geometry_consistent": fallback_geometry_consistent,
                    "outer_columns": (
                        fallback_review.get("outer_columns")
                        if isinstance(fallback_review, dict)
                        else None
                    ),
                    "outer_rows": (
                        fallback_review.get("outer_rows")
                        if isinstance(fallback_review, dict)
                        else None
                    ),
                    "reason": fallback_reason[:2000],
                }
            )
            if not fallback_review_passed:
                record["candidate_options"].append(
                    {
                        "strategy": "full_page",
                        "accepted": False,
                        "error": "independent nested-layout review rejected candidate",
                        "grafts": [],
                    }
                )
                record["error"] = "all nested-layout-reviewed candidates were rejected"
                results.append(record)
                continue
            fallback_sequence = 0.50

        grafted, grafts = graft_improved_tables(
            original,
            candidate,
            min_sequence_similarity=fallback_sequence,
        )
        options = [("full_page", candidate, [])]
        if grafts:
            options.append(("table_graft", grafted, grafts))

        evaluated = []
        for strategy, option, option_grafts in options:
            accepted, metrics = candidate_improvement(
                original,
                option,
                min_table_sequence_similarity=fallback_sequence,
            )
            evaluated.append(
                {
                    "strategy": strategy,
                    "accepted": accepted,
                    "metrics": metrics,
                    "grafts": option_grafts,
                    "candidate": option,
                }
            )
        record["candidate_options"].extend(
            {key: value for key, value in item.items() if key != "candidate"}
            for item in evaluated
        )
        accepted_options = [item for item in evaluated if item["accepted"]]
        if not accepted_options:
            record["error"] = "candidate did not meet lossless improvement gates"
            results.append(record)
            logger.warning(
                "%s targeted table-quality repair rejected: %s",
                stem,
                record["candidate_options"],
            )
            continue
        selected = max(
            accepted_options,
            key=lambda item: (
                item["metrics"]["old_problem_tables"] - item["metrics"]["new_problem_tables"],
                item["strategy"] == "table_graft",
                item["metrics"]["coverage"],
                item["metrics"]["order_similarity"],
                -abs(item["metrics"]["char_ratio"] - 1.0),
            ),
        )
        persist_page(stem, json.dumps(selected["candidate"], ensure_ascii=False))
        record["accepted"] = True
        record["strategy"] = selected["strategy"]
        record["metrics"] = selected["metrics"]
        if selected["grafts"]:
            record["grafts"] = selected["grafts"]
        results.append(record)
        logger.info(
            "%s targeted table-quality repair accepted (%s): %s",
            stem,
            selected["strategy"],
            selected["metrics"],
        )
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
