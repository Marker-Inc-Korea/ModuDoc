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
from pathlib import Path

from bs4 import BeautifulSoup

import table_validate


logger = logging.getLogger(__name__)

TABLE_QUALITY_REPAIR = os.environ.get("TABLE_QUALITY_REPAIR", "1") == "1"
TABLE_QUALITY_REPAIR_MAX_PAGES = max(0, int(os.environ.get("TABLE_QUALITY_REPAIR_MAX_PAGES", "8")))
TABLE_QUALITY_REPAIR_TABLE_ATTEMPTS = min(
    3, max(1, int(os.environ.get("TABLE_QUALITY_REPAIR_TABLE_ATTEMPTS", "2")))
)
TABLE_QUALITY_REPAIR_MAX_TOKENS = max(1024, int(os.environ.get("TABLE_QUALITY_REPAIR_MAX_TOKENS", "16384")))
TABLE_QUALITY_REPAIR_TIMEOUT = max(60, int(os.environ.get("TABLE_QUALITY_REPAIR_TIMEOUT", "600")))
TABLE_QUALITY_REPAIR_IMG_MAXW = max(1024, int(os.environ.get("TABLE_QUALITY_REPAIR_IMG_MAXW", "2464")))
TABLE_QUALITY_REPAIR_TRIM_WHITESPACE = (
    os.environ.get("TABLE_QUALITY_REPAIR_TRIM_WHITESPACE", "1") == "1"
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
- Every table must expand to a rectangle after colspan/rowspan. Do not add an all-empty padding column.
- Preserve blank cells bounded by visible grid lines, including trailing blank header cells; do not absorb them into a neighboring colspan.
- Preserve every visually merged header and body cell with the corresponding
  colspan or rowspan. Never shift values between columns or omit trailing cells.
- Return valid JSON without markdown fences or commentary."""


TABLE_ONLY_SYSTEM_PROMPT = """You repair malformed table geometry in document images.
Return ONLY one valid JSON object:
{"page_number": int, "elements": [{"type": "table", "content": "<table>...</table>", "caption": ""}]}

Rules:
- Locate only the grid(s) corresponding to the supplied broken table reference.
- Return only corrected table elements, not headings or surrounding page prose.
- Use the image as truth; the supplied cell strings are a text-preservation reference, not a geometry reference.
- The supplied expanded row widths and most-frequent width are diagnostics from
  the old HTML. Use them to locate malformed colspan/rowspan, but let visible
  borders in the image override the hint.
- Preserve all visible cells and their reading order. Do not summarize or silently correct wording.
- Use only <table>, <tr>, <td>, <br>, colspan, and rowspan. Do not use <th>.
- Count the visible top-level cell regions in each row before assigning spans, then expand every row mentally and ensure it resolves to the same column count.
- Keep every blank cell bounded by visible grid lines, including leading and trailing blank header cells. Never absorb such a cell into a neighboring colspan.
- Preserve merged headers and genuinely nested grids with correct colspan/rowspan.
- Separate visually separate grids. Never add an all-empty padding column.
- If the supplied reference is not a real bordered grid in the image, return an empty elements array.
- Output no markdown fences or commentary."""


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
    quality = table_validate.assess_table_quality(
        candidate.get("content"), candidate.get("caption")
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
    }
    accepted = bool(
        old_problems
        and len(new_problems) < len(old_problems)
        and new_tables >= old_tables
        and coverage >= TABLE_QUALITY_REPAIR_MIN_COVERAGE
        and char_ratio >= TABLE_QUALITY_REPAIR_MIN_COVERAGE
        and order_similarity >= 0.80
        and table_sequence_preserved
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


def _encode_image(path: Path) -> str:
    from PIL import Image

    with Image.open(path) as image:
        image = image.convert("RGB")
        image = _crop_content_bbox(image)
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
        "Return only the corresponding corrected table grid(s) from the image."
    )
    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": TABLE_ONLY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{_encode_image(image_path)}"},
                    },
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
            if source_indexes != list(range(len(cells))):
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
        record["candidate_options"] = []
        targeted_accepted = False
        for attempt in range(1, TABLE_QUALITY_REPAIR_TABLE_ATTEMPTS + 1):
            try:
                table_candidate = _request_table_candidate(
                    client, model, image_path, original, problems
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
                    0.80
                    if targeted_strategy == "grid_corrected_table_graft"
                    else 0.98
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
        grafted, grafts = graft_improved_tables(original, candidate)
        options = [("full_page", candidate, [])]
        if grafts:
            options.append(("table_graft", grafted, grafts))

        evaluated = []
        for strategy, option, option_grafts in options:
            accepted, metrics = candidate_improvement(original, option)
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
