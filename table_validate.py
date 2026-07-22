"""표 HTML 검증·수리: side-by-side 분할, column-bleed 병합, ragged 행 패딩.
수리 불가 시 needs_retry=True. 의존: bs4 + statistics."""
import copy
import re
import statistics
from collections import Counter
from difflib import SequenceMatcher
from functools import lru_cache
from html import escape
from bs4 import BeautifulSoup

_CONT = "\x00C\x00"   # rowspan/colspan 연속칸 센티넬

def _norm(t):
    return re.sub(r"\s+", "", t or "").strip()

def _rows_of(table):
    """이 table 의 직속 tr 만(중첩 <table> 의 tr 제외)."""
    out = []
    for tr in table.find_all("tr"):
        anc = tr.find_parent("table")
        if anc is table:
            out.append(tr)
    return out

def _build_grid(rows):
    """rowspan 캐리 반영 점유 그리드. 반환 (grid[list[dict]], row_width[list], R, C)."""
    grid, row_width = [], []
    carry = {}   # col -> 남은 행수(rowspan 연속)
    for tr in rows:
        gridrow = {}
        c = 0
        cells = tr.find_all(["td", "th"], recursive=False) or tr.find_all(["td", "th"])
        for td in cells:
            while carry.get(c, 0) > 0:
                gridrow[c] = _CONT
                carry[c] -= 1
                c += 1
            try:
                cs = max(1, int(td.get("colspan", 1) or 1))
                rs = max(1, int(td.get("rowspan", 1) or 1))
            except (ValueError, TypeError):
                cs = rs = 1
            text = td.get_text(" ", strip=True)
            gridrow[c] = text
            for cc in range(c + 1, c + cs):
                gridrow[cc] = _CONT
            if rs > 1:
                for cc in range(c, c + cs):
                    carry[cc] = rs - 1
            c += cs
        while carry.get(c, 0) > 0:
            gridrow[c] = _CONT
            carry[c] -= 1
            c += 1
        for col in list(carry.keys()):
            if col not in gridrow and carry[col] > 0:
                gridrow[col] = _CONT
                carry[col] -= 1
        width = (max(gridrow) + 1) if gridrow else 0
        gridrow = {k: gridrow.get(k, "") for k in range(width)}
        grid.append(gridrow)
        row_width.append(width)
    C = max(row_width) if row_width else 0
    return grid, row_width, len(rows), C

def _grid_to_html(grid, lo, hi, caption=None):
    """grid 의 [lo,hi) 열 범위를 새 <table> HTML 로."""
    out = ["<table>"]
    for gr in grid:
        out.append("<tr>")
        for col in range(lo, hi):
            v = gr.get(col, "")
            if v == _CONT:
                out.append("<td></td>")
            else:
                out.append(f"<td>{escape(str(v))}</td>")
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)

def _has_colspan_crossing(grid, seam):
    """seam 경계를 가로지르는 가로병합 존재 여부."""
    for gr in grid:
        col = 0
        n = max(gr) + 1 if gr else 0
        while col < n:
            if gr.get(col) not in ("", _CONT):
                span_end = col + 1
                while span_end < n and gr.get(span_end) == _CONT:
                    span_end += 1
                if col < seam <= span_end - 1:
                    return True
                col = span_end
            else:
                col += 1
    return False


def _direct_cells(row):
    return row.find_all(["td", "th"], recursive=False)


def _has_possible_cross_row_bleed(rows):
    """Detect a row label repeated near the end of the preceding row's last cell.

    This is only a review signal. It does not alter table text because a repeated
    label can be legitimate; the image-backed quality pass decides whether a
    cell boundary actually needs correction.
    """
    for previous, current in zip(rows, rows[1:]):
        previous_cells = _direct_cells(previous)
        current_cells = _direct_cells(current)
        if (
            len(previous_cells) < 2
            or len(previous_cells) != len(current_cells)
        ):
            continue
        previous_tail = _norm(
            previous_cells[-1].get_text(" ", strip=True)
        ).casefold()
        current_label = _norm(
            current_cells[0].get_text(" ", strip=True)
        ).casefold()
        if (
            not (3 <= len(current_label) <= 40)
            or sum(char.isalpha() for char in current_label) < 2
            or len(previous_tail) < len(current_label) + 8
        ):
            continue
        position = previous_tail.rfind(current_label)
        chars_after = len(previous_tail) - position - len(current_label)
        if position >= 0 and chars_after <= 8:
            return True
    return False


def _has_internal_near_duplicate(rows):
    """Flag near-identical substantial lines repeated inside one cell."""
    for row in rows:
        for cell in _direct_cells(row):
            values = []
            for fragment in cell.stripped_strings:
                normalized = "".join(
                    char.casefold() for char in str(fragment) if char.isalnum()
                )
                if len(normalized) >= 16:
                    values.append(normalized)
            for left_index, left in enumerate(values):
                for right in values[left_index + 1:]:
                    matcher = SequenceMatcher(None, left, right, autojunk=False)
                    if (
                        matcher.ratio() >= 0.86
                        and matcher.find_longest_match().size
                        >= int(min(len(left), len(right)) * 0.70)
                    ):
                        return True
    return False


def _span_int(cell, attr, default=1):
    try:
        return max(1, int(cell.get(attr, default) or default))
    except (TypeError, ValueError):
        return default


def _rowspan_crosses(rows, seam):
    for row_index, row in enumerate(rows[:seam]):
        for cell in _direct_cells(row):
            if row_index + _span_int(cell, "rowspan") > seam:
                return True
    return False


def _split_stacked_tables(rows, row_width, caption=None):
    """Split stacked grids when each side has a stable, different width."""
    if len(rows) < 4 or len(set(row_width)) < 2:
        return None
    candidates = []
    for seam in range(2, len(rows) - 1):
        upper = row_width[:seam]
        lower = row_width[seam:]
        upper_mode = statistics.mode(upper)
        lower_mode = statistics.mode(lower)
        if upper_mode < 2 or lower_mode < 2 or upper_mode == lower_mode:
            continue
        upper_ratio = upper.count(upper_mode) / len(upper)
        lower_ratio = lower.count(lower_mode) / len(lower)
        if upper_ratio < 0.8 or lower_ratio < 0.8 or _rowspan_crosses(rows, seam):
            continue
        candidates.append((upper_ratio + lower_ratio, seam))
    if not candidates:
        return None
    _, seam = max(candidates)

    def _table_html(part):
        return "<table>" + "".join(str(row) for row in part) + "</table>"

    return [
        {"content": _table_html(rows[:seam]), "caption": caption},
        {"content": _table_html(rows[seam:]), "caption": caption},
    ]


def _nested_tables_are_rectangular(table):
    nested = [item for item in table.find_all("table") if item.find_parent("table") is table]
    if not nested:
        return True
    for item in nested:
        rows = _rows_of(item)
        _, widths, _, cols = _build_grid(rows)
        if not rows or cols < 1 or len(set(widths)) > 1:
            return False
    return True


def validate_and_repair_table(html, caption=None):
    """반환 (elements:list[{'content':html,'caption':...}], needs_retry:bool, issues:list).
    elements 가 2개면 분할 결과."""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        tables = soup.find_all("table")
        if not tables:
            return [{"content": html, "caption": caption}], False, []
        top = tables[0]
        if top.find("table"):
            rows = _rows_of(top)
            _, row_width, _, _ = _build_grid(rows)
            issues = []
            if len(set(row_width)) > 1:
                issues.append("ragged_rows")
            if _nested_tables_are_rectangular(top):
                issues.append("nested_table_kept")
            else:
                issues.append("nested_table")
            return (
                [{"content": html, "caption": caption}],
                bool(set(issues) & {"ragged_rows", "nested_table"}),
                issues,
            )
        rows = _rows_of(top)
        if len(rows) < 2:
            return [{"content": html, "caption": caption}], False, []
        grid, row_width, R, C = _build_grid(rows)
        if C < 2:
            return [{"content": html, "caption": caption}], False, []

        stacked = _split_stacked_tables(rows, row_width, caption)
        if stacked:
            return stacked, False, ["stacked_tables_split"]

        # 병합셀(colspan/rowspan>1) 표는 수리 제외(원본 유지). 무병합 표만 아래 수리.
        has_merge = any(
            (_span_int(c, "colspan") > 1 or _span_int(c, "rowspan") > 1)
            for c in top.find_all(["td", "th"])
        )
        if has_merge:
            return [{"content": html, "caption": caption}], False, ["has_merge_keep"]

        issues = []
        canonical = statistics.mode(row_width) if row_width else C
        for r, w in enumerate(row_width):
            if w != canonical:
                issues.append(("ragged_row", r, w - canonical))

        # --- side-by-side 분할(가드: C>=4) ---
        split_at = None
        if C >= 4:
            # (4a) gutter: 가운데 빈 열 + 좌/우 내용
            for c in range(1, C - 1):
                col_all_empty = all(_norm(grid[r].get(c, "")) == "" or grid[r].get(c) == _CONT for r in range(R))
                left_has = any(_norm(grid[r].get(c - 1, "")) for r in range(R))
                right_has = any(_norm(grid[r].get(c + 1, "")) for r in range(R))
                if col_all_empty and left_has and right_has:
                    if not _has_colspan_crossing(grid, c):
                        split_at = ("gutter", c, c + 1); break
            # (4b) tiled header: 헤더행 좌반복==우반복
            if split_at is None and R >= 2:
                hdr = [_norm(grid[0].get(c, "")) for c in range(C)]
                for k in range(2, C // 2 + 1):
                    if hdr[0:k] == hdr[k:2 * k] and any(hdr[0:k]):
                        if 2 * k == C and not _has_colspan_crossing(grid, k):
                            split_at = ("tiled", k, k); break

        if split_at:
            kind, lo_hi, rstart = split_at
            if kind == "gutter":
                left = _grid_to_html(grid, 0, lo_hi)
                right = _grid_to_html(grid, rstart, C)
            else:
                left = _grid_to_html(grid, 0, lo_hi)
                right = _grid_to_html(grid, lo_hi, C)
            # 양쪽 모두 ≥2열·≥2 비어있지않은 행일 때만 분할
            def _ok(h):
                g, rw, r2, c2 = _build_grid(_rows_of(BeautifulSoup(h, "html.parser").find("table")))
                nonempty = sum(1 for gr in g if any(_norm(v) for v in gr.values()))
                # A blank column inside one continuous score/data matrix is not
                # proof of two side-by-side tables. Require independent textual
                # labels on both halves before severing row associations.
                labels = sum(
                    1 for gr in g for value in gr.values()
                    if value != _CONT
                    and sum(char.isalpha() for char in str(value or "")) >= 2
                )
                return c2 >= 2 and nonempty >= 2 and labels >= 2
            if _ok(left) and _ok(right):
                issues.append(
                    ("gutter", lo_hi, 0) if kind == "gutter"
                    else ("tiled_header", lo_hi, 0)
                )
                return ([{"content": left, "caption": caption},
                         {"content": right, "caption": (caption + " (우)") if caption else None}],
                        False, issues)

        # --- column-bleed: 인접 동일값 + canonical+1 행이면 중복 1개 병합 ---
        repaired = False
        for r in range(R):
            if row_width[r] == canonical + 1:
                for c in range(C - 1):
                    a, b = _norm(grid[r].get(c, "")), _norm(grid[r].get(c + 1, ""))
                    if a and a == b and grid[r].get(c + 1) != _CONT:
                        for cc in range(c + 1, C - 1):
                            grid[r][cc] = grid[r].get(cc + 1, "")
                        grid[r].pop(C - 1, None)
                        row_width[r] -= 1
                        issues.append(("adjacent_dup_merged", r, c)); repaired = True
                        break

        # --- ragged 짧은 행 패딩 ---
        for r in range(R):
            if row_width[r] < canonical:
                for c in range(row_width[r], canonical):
                    grid[r][c] = ""
                row_width[r] = canonical
                repaired = True

        post_ragged = sum(1 for w in row_width if w != statistics.mode(row_width))
        needs_retry = post_ragged > 0.10 * max(1, R)

        if repaired:
            return [{"content": _grid_to_html(grid, 0, max(row_width)), "caption": caption}], needs_retry, issues
        return [{"content": html, "caption": caption}], needs_retry, issues
    except Exception:
        return [{"content": html, "caption": caption}], False, ["validator_error"]


def assess_table_quality(html, caption=None, allow_nested=False):
    """Return lightweight table quality signals for provenance/confidence metadata."""
    out = {"confidence": 0.95, "issues": [], "rows": 0, "cols": 0}
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        table = soup.find("table")
        if table is None:
            out["confidence"] = 0.0
            out["issues"].append("no_table")
            return out
        if (
            table.find("table") is not None
            and not allow_nested
            and not _nested_tables_are_rectangular(table)
        ):
            out["issues"].append("nested_table")
            out["confidence"] = min(out["confidence"], 0.65)
        rows = _rows_of(table)
        grid, row_width, R, C = _build_grid(rows)
        out["rows"], out["cols"] = R, C
        if not rows:
            out["confidence"] = 0.2
            out["issues"].append("empty_table")
            return out
        if len(set(row_width)) > 1:
            out["issues"].append("ragged_rows")
            out["confidence"] = min(out["confidence"], 0.65)
        if table.find("th") is not None:
            out["issues"].append("th_tags_present")
            out["confidence"] = min(out["confidence"], 0.90)
        if _has_possible_cross_row_bleed(rows):
            out["issues"].append("possible_cross_row_bleed")
        if _has_internal_near_duplicate(rows):
            out["issues"].append("possible_internal_duplicate_text")

        return out
    except Exception:
        out["confidence"] = 0.4
        out["issues"].append("quality_check_error")
        return out


# ───────────────────────── HWP 네이티브 표 치환 ─────────────────────────
# 내용이 일치하는 표를 HWP/HWPX 네이티브 표(rhwp IR TableBlock.html)로 치환.
# 페이지 분할표는 본문행을 키로 슬라이스해 네이티브 헤더와 합침.
_TILDE = str.maketrans({"～": "~", "∼": "~", "〜": "~", "˜": "~"})
def _ncell(s):
    # 셀 정규화: 공백 제거·물결 통일·소문자.
    return re.sub(r"\s+", "", (s or "").translate(_TILDE)).lower()

def _cell_set(html):
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return set()
    return {_ncell(c.get_text()) for c in soup.find_all(["td", "th"]) if c.get_text(strip=True)}

def prepare_native(native_list):
    """네이티브 표 목록(dict: html,rows,cols)에 매칭용 셀집합·셀수 부착(빈표 제외)."""
    out = []
    for index, nt in enumerate(native_list or []):
        h = (nt.get("html") or "").strip()
        cs = _cell_set(h)
        if len(cs) >= 2:
            prepared = {
                "html": h,
                "_set": cs,
                "_n": len(cs),
                "_index": index,
            }
            for key in ("rows", "cols"):
                if isinstance(nt.get(key), int) and nt[key] > 0:
                    prepared[key] = nt[key]
            out.append(prepared)
    return out

def _split_header_body(html):
    soup = BeautifulSoup(html or "", "html.parser")
    t = soup.find("table")
    if not t:
        return [], []
    rows = [tr for tr in t.find_all("tr") if tr.find_parent("table") is t]
    hdr = [tr for tr in rows if tr.find("th")]
    body = [tr for tr in rows if not tr.find("th")]
    if not hdr and rows:          # th 없으면 첫 행을 헤더로
        hdr, body = rows[:1], rows[1:]
    return hdr, body

def _row_key(tr):
    c = tr.find(["td", "th"])
    return _ncell(c.get_text()) if c else ""

def _rspan(cell):
    try:
        return max(1, int(cell.get("rowspan") or 1))
    except (ValueError, TypeError):
        return 1

def _slice_native(native_html, vlm_html):
    """Align a page fragment to one contiguous, ordered native-table slice."""
    nh, nb = _split_header_body(native_html)
    _vh, vb = _split_header_body(vlm_html)
    keyed_vlm_rows = [
        (index, key)
        for index, tr in enumerate(vb)
        if (key := _row_key(tr))
    ]
    native_keys = [_row_key(tr) for tr in nb]
    if not keyed_vlm_rows or not native_keys:
        return None

    vlm_keys = [key for _, key in keyed_vlm_rows]

    def mapping_score(pairs):
        if not pairs:
            return (0, 0, 0)
        native_indices = [pair[1] for pair in pairs]
        span = native_indices[-1] - native_indices[0] + 1
        return (len(pairs), -(span - len(pairs)), -span)

    @lru_cache(maxsize=None)
    def align(vlm_index, native_index):
        if vlm_index >= len(vlm_keys) or native_index >= len(native_keys):
            return ()
        options = [
            align(vlm_index + 1, native_index),
            align(vlm_index, native_index + 1),
        ]
        if vlm_keys[vlm_index] == native_keys[native_index]:
            options.append(
                ((vlm_index, native_index),)
                + align(vlm_index + 1, native_index + 1)
            )
        return max(options, key=mapping_score)

    matches = align(0, 0)
    minimum_matches = max(2, (len(vlm_keys) + 1) // 2)
    if len(matches) < minimum_matches:
        return None
    native_start = matches[0][1]
    native_end = matches[-1][1]
    native_span = native_end - native_start + 1
    if native_span > max(len(matches) + 2, int(len(matches) * 1.5)):
        return None

    # Keep complete rowspan groups and every row between the first and last
    # ordered anchor. Missing an interior VLM row must not drop native content.
    keep_idx = set(range(native_start, native_end + 1))
    cursor = 0
    for group in _rowspan_groups(nb):
        group_indices = set(range(cursor, cursor + len(group)))
        if group_indices & keep_idx:
            keep_idx.update(group_indices)
        cursor += len(group)
    keep = [tr for index, tr in enumerate(nb) if index in keep_idx]

    matched_vlm_positions = [keyed_vlm_rows[index][0] for index, _ in matches]
    first_vlm = min(matched_vlm_positions)
    last_vlm = max(matched_vlm_positions)
    native_text = _ncell(" ".join(tr.get_text(" ", strip=True) for tr in keep))

    def dominant_native_span_signature():
        _, _, _, native_width = _build_grid(nb)
        signatures = []
        for row in nb:
            cells = _direct_cells(row)
            if len(cells) < 2 or any(_rspan(cell) != 1 for cell in cells):
                continue
            signature = tuple(_span_int(cell, "colspan") for cell in cells)
            if sum(signature) == native_width:
                signatures.append(signature)
        if not signatures:
            return None
        counts = Counter(signatures).most_common()
        if counts[0][1] < 2 or (len(counts) > 1 and counts[0][1] == counts[1][1]):
            return None
        return counts[0][0]

    span_signature = dominant_native_span_signature()

    def align_boundary_spans(row):
        """Restore a seam row's spans from a stable native data-row shape."""
        if not span_signature:
            return row
        cells = _direct_cells(row)
        if (
            len(cells) != len(span_signature)
            or any(_rspan(cell) != 1 for cell in cells)
            or any(_span_int(cell, "colspan") != 1 for cell in cells)
            or sum(span_signature) == len(cells)
        ):
            return row
        for cell, colspan in zip(cells, span_signature):
            if colspan == 1:
                cell.attrs.pop("colspan", None)
            else:
                cell["colspan"] = str(colspan)
        return row

    def boundary_rows(rows):
        retained = []
        for row in rows:
            row_text = _ncell(row.get_text(" ", strip=True))
            if row_text and not _row_key(row) and row_text not in native_text:
                retained.append(align_boundary_spans(row))
        return retained

    leading = boundary_rows(vb[:first_vlm])
    trailing = boundary_rows(vb[last_vlm + 1 :])
    return (
        "<table>"
        + "".join(str(tr) for tr in nh)
        + "".join(str(tr) for tr in leading)
        + "".join(str(tr) for tr in keep)
        + "".join(str(tr) for tr in trailing)
        + "</table>"
    )

def _cell_own_text(cell):
    """셀의 텍스트(중첩 <table> 내용 제외) 정규화."""
    frag = BeautifulSoup(str(cell), "html.parser")
    for t in frag.find_all("table"):
        t.decompose()
    return _ncell(frag.get_text(" ", strip=True))

def _outer_own_cellset(table):
    """outer table 직속 셀들의 own-text 집합(중첩표 내용 제외)."""
    s = set()
    for c in table.find_all(["td", "th"]):
        if c.find_parent("table") is table:
            t = _cell_own_text(c)
            if t:
                s.add(t)
    return s

def _nested_in_outer(table):
    """outer table 셀 안의 직속 중첩표 [(셀, 중첩table)]."""
    out = []
    for c in table.find_all(["td", "th"]):
        if c.find_parent("table") is not table:
            continue
        for nt in c.find_all("table"):
            if nt.find_parent("table") is table:
                out.append((c, nt))
    return out

def _cap_of(tab):
    c = tab.find(["th", "td"])
    return _ncell(c.get_text(" ", strip=True)) if c else ""

def _seq_ratio(a, b):
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _ordered_table_text(html):
    try:
        table = BeautifulSoup(html or "", "html.parser").find("table")
    except Exception:
        return ""
    return _ncell(table.get_text(" ", strip=True)) if table else ""


def _ordered_native_match(vlm_html, native_prepared):
    """Match split-cell VLM output to one uniquely similar native table."""
    vlm_text = _ordered_table_text(vlm_html)
    if len(vlm_text) < 80:
        return None
    ranked = []
    for native in native_prepared:
        native_text = _ordered_table_text(native.get("html"))
        if not native_text:
            continue
        length_ratio = min(len(vlm_text), len(native_text)) / max(
            len(vlm_text), len(native_text)
        )
        if length_ratio < 0.90:
            continue
        similarity = SequenceMatcher(
            None, vlm_text, native_text, autojunk=False
        ).ratio()
        ranked.append((similarity, length_ratio, native))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best_similarity, _, best = ranked[0]
    if best_similarity < 0.97:
        return None
    if len(ranked) > 1:
        second_similarity = ranked[1][0]
        if second_similarity >= 0.80 and best_similarity - second_similarity < 0.05:
            return None
    return best["html"]

def _graft_nested_native(vlm_html, native_prepared):
    """중첩표 VLM 표를 native 골격(열수가 더 많은 정답 구조)에 접합해 반환(불가 시 None).
    중첩표 캡션으로 native 표·삽입 셀을 특정, 중첩표는 소실 없이 그대로 이식."""
    try:
        vtable = BeautifulSoup(vlm_html or "", "html.parser").find("table")
    except Exception:
        return None
    if vtable is None:
        return None
    nested = _nested_in_outer(vtable)
    if not nested:
        return None
    _g, _w, _R, vcols = _build_grid(_rows_of(vtable))
    vset = _outer_own_cellset(vtable)
    if len(vset) < 2:
        return None
    # base(native) 선택: outer 셀집합 포함도 최대(중첩표 첫셀 캡션보다 안정적).
    base, best = None, 0.0
    for nt in native_prepared:
        ov = len(vset & nt["_set"]) / len(vset)
        if ov > best:
            base, best = nt, ov
    if base is None or best < 0.5:
        return None
    try:
        ntable = BeautifulSoup(base["html"], "html.parser").find("table")
        _ng, _nw, _nR, ncols = _build_grid(_rows_of(ntable))
    except Exception:
        return None
    if ncols <= vcols:                      # native가 열을 더 가질 때만(붕괴된 열 복원)
        return None
    ncells = [c for c in ntable.find_all(["td", "th"]) if c.find_parent("table") is ntable]
    placed = 0
    for vcell, ntab in nested:
        # 삽입 대상 native 셀: 중첩표를 품은 VLM 셀의 텍스트(중첩표 제외)와 최대 유사 셀.
        anchor = _cell_own_text(vcell)
        target = max(ncells, key=lambda c: _seq_ratio(anchor, _cell_own_text(c)), default=None)
        if target is None or _seq_ratio(anchor, _cell_own_text(target)) < 0.5:
            cap = _cap_of(ntab)                 # 보조: 중첩표 첫 셀 캡션 부분매칭
            target = next((c for c in ncells if cap and len(cap) >= 4 and cap in _cell_own_text(c)), None)
        if target is not None:
            target.append(BeautifulSoup(str(ntab), "html.parser"))
            placed += 1
    if placed != len(nested):               # 하나라도 못 넣으면 포기(중첩표 소실 방지)
        return None
    # 무손실 게이트: graft 로 사라지는 VLM outer 셀(=native 골격에 없는 셀)의 실질 내용이
    # native 전체 텍스트에 실재해야 교체(재구조화는 통과, native 축약본 교체는 거부).
    gtext = _ncell(ntable.get_text(" ", strip=True))
    gset = {_ncell(c.get_text(" ", strip=True)) for c in ntable.find_all(["td", "th"]) if c.get_text(strip=True)}
    for c in vset:
        if len(c) < 8 or c in gset:
            continue
        toks = re.findall(r"[가-힣A-Za-z0-9]{4,}", c)     # 셀의 실체 토큰(불릿/기호 배제)
        if toks and sum(t in gtext for t in toks) < len(toks) * 0.7:
            return None                                    # 실체 다수가 native 부재 → 손실
    return str(ntable)

def _native_substitute_with_slice_status(
    vlm_html, native_prepared, min_score=0.6
):
    """Return a native match and whether it was cut from a larger table."""
    if not native_prepared:
        return None, False
    # 중첩표(표 안의 표): native 골격에 중첩표 접합 시도(불가 시 원본 유지).
    try:
        _t = BeautifulSoup(vlm_html or "", "html.parser").find("table")
        if _t is not None and _t.find("table") is not None:
            return _graft_nested_native(vlm_html, native_prepared), False
    except Exception:
        pass
    vset = _cell_set(vlm_html)
    if len(vset) < 4:
        return None, False
    best, best_score = None, 0.0
    for nt in native_prepared:
        score = len(vset & nt["_set"]) / len(vset)
        if score > best_score:
            best, best_score = nt, score
    if best is None or best_score < min_score:
        return _ordered_native_match(vlm_html, native_prepared), False
    if best["_n"] > len(vset) * 1.5:          # 분할표 → 슬라이스
        return _slice_native(best["html"], vlm_html), True
    return best["html"], False


def native_substitute(vlm_html, native_prepared, min_score=0.6):
    """내용 일치하는 네이티브 표 HTML 반환(없으면 None).
    셀집합 포함도(교집합/VLM셀수) 임계 이상, 네이티브가 훨씬 크면 슬라이스."""
    matched, _ = _native_substitute_with_slice_status(
        vlm_html, native_prepared, min_score
    )
    return matched


def native_substitute_for_source_page(
    vlm_html, native_prepared, source_text, min_score=0.6
):
    """Return a page-grounded native match, or None to retain the VLM table."""
    matched, was_sliced = _native_substitute_with_slice_status(
        vlm_html, native_prepared, min_score
    )
    if not matched:
        return None
    source_sliced = slice_native_to_source_page(matched, source_text)
    was_sliced = was_sliced or source_sliced != matched
    if was_sliced and not native_page_slice_is_source_grounded(
        source_sliced, source_text
    ):
        return None
    return source_sliced


def _ordered_source_supported_rows(rows, normalized_source):
    """Align native rows to source occurrences without reusing one anchor."""
    row_candidates = []
    for index, row in enumerate(rows):
        values = [
            value
            for cell in row.find_all(["td", "th"], recursive=False)
            if (value := _cell_own_text(cell)) and len(value) >= 3
        ]
        exact_values = [value for value in values if value in normalized_source]
        if not exact_values:
            row_candidates.append((index, []))
            continue
        anchor = max(exact_values, key=len)
        positions = []
        start = 0
        while True:
            position = normalized_source.find(anchor, start)
            if position < 0:
                break
            positions.append((position, position + len(anchor)))
            start = position + 1
        exact_score = sum(len(value) for value in exact_values)
        row_candidates.append(
            (index, [(start, end, exact_score) for start, end in positions])
        )

    # state: last source offset -> (matched rows, exact-text score, row indexes)
    states = {-1: (0, 0, ())}
    for row_index, candidates in row_candidates:
        updated = dict(states)
        for last_end, state in states.items():
            for start, end, score in candidates:
                if start < last_end:
                    continue
                candidate = (state[0] + 1, state[1] + score, state[2] + (row_index,))
                if candidate[:2] > updated.get(end, (-1, -1, ()))[:2]:
                    updated[end] = candidate
        states = updated
    return list(max(states.values(), key=lambda state: state[:2])[2])


def slice_native_to_source_page(native_html, source_text):
    """Trim a long native table to the uniquely supported page-row run."""
    normalized_source = _ncell(source_text)
    if len(normalized_source) < 40:
        return native_html
    try:
        soup = BeautifulSoup(native_html or "", "html.parser")
        table = soup.find("table")
    except Exception:
        return native_html
    rows = _rows_of(table) if table else []
    if len(rows) < 8:
        return native_html

    header_count = 0
    for row in rows:
        if row.find("th", recursive=False) is None:
            break
        header_count += 1
    supported = [
        header_count + index
        for index in _ordered_source_supported_rows(
            rows[header_count:], normalized_source
        )
    ]
    if len(supported) < 2:
        return native_html

    runs = []
    for index in supported:
        if not runs or index - runs[-1][-1] > 3:
            runs.append([])
        runs[-1].append(index)
    ranked = sorted(
        runs,
        key=lambda run: (len(run), -(run[-1] - run[0] + 1)),
        reverse=True,
    )
    best = ranked[0]
    if len(best) < 2 or (
        len(ranked) > 1 and len(best) < len(ranked[1]) + 2
    ):
        return native_html
    start, end = best[0], best[-1]
    keep = [*range(header_count), *range(start, end + 1)]
    if len(rows) - len(set(keep)) < 2:
        return native_html

    rebuilt = soup.new_tag("table")
    for index in keep:
        rebuilt.append(copy.copy(rows[index]))
    quality = assess_table_quality(str(rebuilt), allow_nested=True)
    if set(quality.get("issues") or []) & {
        "no_table",
        "empty_table",
        "ragged_rows",
        "quality_check_error",
    }:
        return native_html
    return str(rebuilt)


def native_page_slice_is_source_grounded(native_html, source_text):
    """Reject native page slices that add substantial text absent from the page."""
    normalized_source = _ncell(source_text)
    if len(normalized_source) < 40:
        return False
    try:
        table = BeautifulSoup(native_html or "", "html.parser").find("table")
    except Exception:
        return False
    if table is None:
        return False
    for cell in table.find_all(["td", "th"]):
        if cell.find_parent("table") is not table:
            continue
        value = _cell_own_text(cell)
        if len(value) < 8 or value in normalized_source:
            continue
        longest = SequenceMatcher(
            None, value, normalized_source, autojunk=False
        ).find_longest_match(0, len(value), 0, len(normalized_source)).size
        if longest / len(value) < 0.90:
            return False
    return True


def strip_caption_duplicate_metadata_row(html, caption):
    """Remove a full-width title/metadata row already retained in caption."""
    if not html or not caption:
        return html
    try:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        rows = _rows_of(table) if table else []
        if len(rows) < 2:
            return html
        first_cells = _direct_cells(rows[0])
        _, _, _, width = _build_grid(rows)
        if (
            len(first_cells) != 1
            or _span_int(first_cells[0], "colspan") != width
            or _rspan(first_cells[0]) != 1
        ):
            return html
        metadata = first_cells[0].get_text(" ", strip=True)
        bracketed = bool(re.fullmatch(
            r"\s*[\(\[\{（［｛【].{1,100}[\)\]\}）］｝】]\s*",
            metadata,
            flags=re.DOTALL,
        ))
        exact_caption = bool(
            _ncell(metadata) and _ncell(metadata) == _ncell(caption)
        )
        next_cells = _direct_cells(rows[1])
        if not (
            (bracketed and _ncell(metadata) in _ncell(caption))
            or (
                exact_caption
                and len(next_cells) >= 2
                and sum(bool(cell.get_text(" ", strip=True)) for cell in next_cells)
                >= 2
            )
        ):
            return html
        rows[0].decompose()
        return str(table)
    except Exception:
        return html


def _extend_edge_rowspans_over_placeholder_row(table):
    """Fold one leaked inner-grid row back under its spanning edge cells."""
    rows = _rows_of(table)
    changed = False
    for row_index, row in enumerate(rows):
        cells = _direct_cells(row)
        if len(cells) < 3:
            continue
        span = _rspan(cells[0])
        if span < 2 or _rspan(cells[-1]) != span:
            continue
        extra_index = row_index + span
        if extra_index >= len(rows):
            continue
        middle_rows = rows[row_index + 1 : extra_index]
        extra_cells = _direct_cells(rows[extra_index])
        if (
            len(extra_cells) < 4
            or extra_cells[0].get_text(" ", strip=True)
            or not extra_cells[-1].get_text(" ", strip=True)
            or any(len(_direct_cells(item)) != len(extra_cells) - 2 for item in middle_rows)
        ):
            continue
        cells[0]["rowspan"] = str(span + 1)
        cells[-1]["rowspan"] = str(span + 1)
        cells[-1].append(BeautifulSoup("<br>", "html.parser").find("br"))
        for child in list(extra_cells[-1].contents):
            cells[-1].append(copy.copy(child))
        extra_cells[0].decompose()
        extra_cells[-1].decompose()
        changed = True
    return changed


def _native_header_geometry(native_html):
    table = BeautifulSoup(native_html or "", "html.parser").find("table")
    rows = _rows_of(table) if table else []
    if not rows:
        return None
    header_cells = _direct_cells(rows[0])
    labels = tuple(_ncell(cell.get_text(" ", strip=True)) for cell in header_cells)
    spans = tuple(_span_int(cell, "colspan") for cell in header_cells)
    _, _, _, width = _build_grid(rows)
    body_signatures = []
    for row in rows[1:]:
        cells = _direct_cells(row)
        signature = tuple(_span_int(cell, "colspan") for cell in cells)
        if len(cells) >= 2 and sum(signature) == width:
            body_signatures.append(signature)
    counts = Counter(body_signatures).most_common()
    if (
        len(labels) < 2
        or not all(labels)
        or sum(spans) != width
        or not counts
        or counts[0][1] < 2
        or (len(counts) > 1 and counts[0][1] == counts[1][1])
    ):
        return None
    return labels, spans, counts[0][0], width


def restore_uniquely_supported_native_parents(
    elements, native_prepared, source_text
):
    """Restore one omitted outer native grid around its adjacent child table.

    This is intentionally strict: every outer-cell label must occur on the
    current page, none may already be represented, and the child must be the
    unique near-exact structured-table match for the next native entry.
    """
    source = list(elements or [])
    normalized_source = _ncell(source_text)
    if not native_prepared or len(normalized_source) < 40:
        return source

    represented = _ncell(
        " ".join(
            BeautifulSoup(str(element.get("content") or ""), "html.parser")
            .get_text(" ", strip=True)
            for element in source
            if isinstance(element, dict)
        )
    )
    by_index = {item.get("_index"): item for item in native_prepared}
    candidates = []
    for parent in native_prepared:
        child = by_index.get(parent.get("_index", -2) + 1)
        if child is None:
            continue
        parent_set = set(parent.get("_set") or [])
        if (
            len(parent_set) < 6
            or sum(map(len, parent_set)) < 40
            or any(value not in normalized_source for value in parent_set)
            or any(value in represented for value in parent_set)
        ):
            continue
        try:
            parent_soup = BeautifulSoup(parent.get("html") or "", "html.parser")
            parent_table = parent_soup.find("table")
            child_table = BeautifulSoup(
                child.get("html") or "", "html.parser"
            ).find("table")
        except Exception:
            continue
        if (
            parent_table is None
            or child_table is None
            or parent_table.find("table") is not None
        ):
            continue
        direct_cells = [
            cell
            for cell in parent_table.find_all(["td", "th"])
            if cell.find_parent("table") is parent_table
        ]
        empty_cells = [
            cell
            for cell in direct_cells
            if not _cell_own_text(cell) and cell.find("table") is None
        ]
        parent_values = {
            value
            for cell in direct_cells
            if (value := _cell_own_text(cell))
        }
        if (
            len(empty_cells) != 1
            or _span_int(empty_cells[0], "colspan") < 2
            or len(parent_values) < 6
            or sum(map(len, parent_values)) < 40
            or any(value not in normalized_source for value in parent_values)
            or any(value in represented for value in parent_values)
        ):
            continue

        parent_rows = _rows_of(parent_table)
        _, parent_widths, _, parent_columns = _build_grid(parent_rows)
        child_rows = _rows_of(child_table)
        _, child_widths, _, child_columns = _build_grid(child_rows)
        if (
            not parent_widths
            or len(set(parent_widths)) != 1
            or parent_columns < 2
            or not child_widths
            or len(set(child_widths)) != 1
            or child_columns < _span_int(empty_cells[0], "colspan")
        ):
            continue

        child_matches = []
        for element_index, element in enumerate(source):
            if not isinstance(element, dict) or element.get("type") != "table":
                continue
            existing_html = element.get("content") or ""
            existing_set = _cell_set(existing_html)
            overlap = len(existing_set & child["_set"])
            if (
                not existing_set
                or overlap / len(child["_set"]) < 0.90
                or overlap / len(existing_set) < 0.90
            ):
                continue
            existing_text = _ordered_table_text(existing_html)
            child_text = _ordered_table_text(child.get("html"))
            if (
                min(len(existing_text), len(child_text))
                / max(1, max(len(existing_text), len(child_text)))
                < 0.90
                or SequenceMatcher(
                    None, existing_text, child_text, autojunk=False
                ).ratio()
                < 0.95
            ):
                continue
            child_matches.append(element_index)
        if len(child_matches) != 1:
            continue

        target = empty_cells[0]
        target.append(copy.copy(child_table))
        restored_html = str(parent_table)
        quality = assess_table_quality(restored_html, allow_nested=True)
        if set(quality.get("issues") or []) & {
            "no_table",
            "empty_table",
            "ragged_rows",
            "quality_check_error",
        }:
            continue
        candidates.append((child_matches[0], restored_html, quality))

    if len(candidates) != 1:
        return source
    element_index, restored_html, quality = candidates[0]
    restored = copy.deepcopy(source)
    element = restored[element_index]
    element["content"] = restored_html
    element["_native"] = True
    element["_source"] = "native_nested_parent_restored"
    element["_confidence"] = min(0.99, quality.get("confidence", 0.99))
    element.pop("_issues", None)
    return restored


def merge_adjacent_native_table_fragments(elements, native_prepared):
    """Join a detached header band to its body using unique native geometry."""
    source = list(elements or [])
    if not native_prepared:
        return source
    result = []
    index = 0
    while index < len(source):
        if index + 1 >= len(source):
            result.append(source[index])
            break
        first, second = source[index], source[index + 1]
        if (
            not isinstance(first, dict)
            or not isinstance(second, dict)
            or first.get("type") != "table"
            or second.get("type") != "table"
            or not _ncell(first.get("caption"))
            or _ncell(first.get("caption")) != _ncell(second.get("caption"))
        ):
            result.append(first)
            index += 1
            continue
        first_table = BeautifulSoup(first.get("content") or "", "html.parser").find("table")
        second_table = BeautifulSoup(second.get("content") or "", "html.parser").find("table")
        first_rows = _rows_of(first_table) if first_table else []
        second_rows = _rows_of(second_table) if second_table else []
        if (
            not (2 <= len(first_rows) <= 3)
            or len(second_rows) < 2
            or len(_direct_cells(first_rows[0])) < 2
            or len(_direct_cells(first_rows[-1])) != 1
        ):
            result.append(first)
            index += 1
            continue
        header_labels = tuple(
            _ncell(cell.get_text(" ", strip=True))
            for cell in _direct_cells(first_rows[0])
        )
        native_matches = []
        for native in native_prepared:
            geometry = _native_header_geometry(native.get("html"))
            if geometry and geometry[0] == header_labels:
                native_matches.append(geometry)
        if len(native_matches) != 1:
            result.append(first)
            index += 1
            continue
        _, header_spans, body_spans, width = native_matches[0]
        merged_soup = BeautifulSoup("<table></table>", "html.parser")
        merged_table = merged_soup.find("table")
        for row in first_rows + second_rows:
            merged_table.append(BeautifulSoup(str(row), "html.parser").find("tr"))
        _extend_edge_rowspans_over_placeholder_row(merged_table)
        merged_rows = _rows_of(merged_table)
        for row_index, row in enumerate(merged_rows):
            cells = _direct_cells(row)
            signature = header_spans if row_index == 0 else body_spans
            if len(cells) == 1:
                cells[0]["colspan"] = str(width)
            elif len(cells) == len(signature):
                for cell, colspan in zip(cells, signature):
                    if colspan == 1:
                        cell.attrs.pop("colspan", None)
                    else:
                        cell["colspan"] = str(colspan)
        _, row_widths, _, _ = _build_grid(merged_rows)
        if not row_widths or len(set(row_widths)) != 1 or row_widths[0] != width:
            result.append(first)
            index += 1
            continue
        merged = {
            key: value for key, value in first.items() if not key.startswith("_")
        }
        merged["content"] = str(merged_table)
        result.append(merged)
        index += 2
    return result


def _table_keyset(html):
    _h, vb = _split_header_body(html)
    return {_row_key(tr) for tr in vb if _row_key(tr)}

def _rowspan_groups(nb):
    """native 본문행을 rowspan 그룹(원자단위)으로 묶음."""
    groups, i = [], 0
    while i < len(nb):
        span = max((_rspan(c) for c in nb[i].find_all(["td", "th"], recursive=False)), default=1)
        end = i + 1
        while end < min(i + span, len(nb)):
            c0 = nb[end].find_all(["td", "th"], recursive=False)
            if c0 and _rspan(c0[0]) > 1:
                break
            end += 1
        groups.append(nb[i:end]); i = end
    return groups

def _gcells(grp):
    s = set()
    for tr in grp:
        for td in tr.find_all(["td", "th"]):
            t = _ncell(td.get_text())
            if t:
                s.add(t)
    return s

def _repartition_rows(native_html, page_keysets, other_cells, homed_keys=None):
    """native 행 그룹을 run 페이지에 1회씩 배정(미배정 페이지는 None).
    매칭→해당 페이지; 미매칭→(a) 행 키가 run 밖 다른 페이지에 실재하거나(homed_keys) 내용 다수가
    인접 페이지(other_cells)에 있으면 그 페이지 소관으로 drop, (b) 어디에도 없는 진짜 seam만 인접
    페이지에 귀속. 다중페이지 요약표의 far-page 행이 경계 페이지로 쏠리는 것을 막는다."""
    homed_keys = homed_keys or set()
    nh, nb = _split_header_body(native_html)
    assign, last, leading = {}, None, []
    for grp in _rowspan_groups(nb):
        pg = None
        grp_key = ""
        for tr in grp:
            k = _row_key(tr)
            if not k:
                continue
            if not grp_key:
                grp_key = k
            for pi, ks in enumerate(page_keysets):
                if k in ks:
                    pg = pi; break
            if pg is not None:
                break
        if pg is not None:
            last = pg
            assign.setdefault(pg, []).extend(grp)
        else:
            if grp_key and grp_key in homed_keys:
                continue                                  # 행 키가 run 밖 페이지에 실재 → 그 페이지 소관, drop
            gc = _gcells(grp)
            if gc and len(gc & other_cells) > len(gc) / 2:
                continue                                  # 내용이 인접 페이지에 존재 → 그 페이지 소관, drop
            if last is None:
                leading.extend(grp)                       # 첫 매칭 전 seam → 보류
            else:
                assign.setdefault(last, []).extend(grp)   # 중간·후행 seam → 인접 페이지
    if leading and assign:
        fp = min(assign)
        assign[fp] = leading + assign[fp]                 # 선행 seam → 첫 매칭 페이지 앞
    return [("<table>" + "".join(str(t) for t in nh) + "".join(str(t) for t in assign[pi]) + "</table>")
            if assign.get(pi) else None for pi in range(len(page_keysets))]

def _safe_runs(items):
    """items=[(pg, keyset), ...]. 연속 페이지 + 인접쌍 키겹침<0.3 인 run(길이>=2)만 반환."""
    def pnum(p):
        m = re.search(r"(\d+)", p)
        return int(m.group(1)) if m else 0
    items = sorted(items, key=lambda x: pnum(x[0]))
    runs, cur = [], [items[0]]
    for prev, nxt in zip(items, items[1:]):
        consec = pnum(nxt[0]) - pnum(prev[0]) == 1
        a, b = prev[1], nxt[1]
        ov = len(a & b) / max(1, min(len(a), len(b))) if a and b else 0
        if consec and ov < 0.3:
            cur.append(nxt)
        else:
            if len(cur) >= 2:
                runs.append(cur)
            cur = [nxt]
    if len(cur) >= 2:
        runs.append(cur)
    return runs

def repartition_native_tables(doc_output_dir):
    """페이지로 분할된 동일 네이티브 표의 행을 연속 페이지에 1회씩 재분배(seam 손실·중복 방지).
    연속+키 disjoint 인 run 만 처리하고 반복·비연속 표는 그대로 둔다."""
    import os, json, glob
    from collections import defaultdict
    npath = os.path.join(doc_output_dir, "_native_tables.json")
    if not os.path.exists(npath):
        return
    try:
        natives = json.load(open(npath, encoding="utf-8"))
    except Exception:
        return
    nsets = [_cell_set(n.get("html", "")) for n in natives]
    pages, by_native, allcells, page_rowkeys = {}, defaultdict(list), {}, {}
    for j in sorted(glob.glob(os.path.join(glob.escape(doc_output_dir), "page_*_structured.json"))):
        pg = os.path.basename(j).replace("_structured.json", "")
        try:
            data = json.load(open(j, encoding="utf-8"))
        except Exception:
            continue
        pages[pg] = (j, data)
        pc, pk = set(), set()
        for k, e in enumerate(data.get("elements", [])):
            if e.get("type") != "table":
                continue
            vs = _cell_set(e.get("content", ""))
            pc |= vs
            pk |= _table_keyset(e.get("content", ""))   # 이 페이지 표들의 행 키(순번 등)
            if not e.get("_native") or not vs:
                continue
            bi, bsc = None, 0.0
            for ni, ns in enumerate(nsets):
                if not ns:
                    continue
                sc = len(vs & ns) / len(vs)
                if sc > bsc:
                    bi, bsc = ni, sc
            if bi is not None and bsc >= 0.6:
                by_native[bi].append((pg, k, _table_keyset(e.get("content", ""))))
        allcells[pg] = pc
        page_rowkeys[pg] = pk
    changed = set()
    for bi, items in by_native.items():
        if len(items) < 2:
            continue
        idx_of = {pg: k for pg, k, _ in items}
        for run in _safe_runs([(pg, ks) for pg, _, ks in items]):
            nums = sorted(int(re.search(r"(\d+)", pg).group(1)) for pg, _ in run)
            adj = (f"page_{nums[0] - 1:04d}", f"page_{nums[-1] + 1:04d}")   # run 직전·직후(분할 spill 소관)
            other = allcells.get(adj[0], set()) | allcells.get(adj[1], set())
            run_pgs = {pg for pg, _ in run}
            homed = set()                                 # run 밖 페이지가 소유한 행 키(그 페이지 소관 → drop)
            for opg, oks in page_rowkeys.items():
                if opg not in run_pgs:
                    homed |= oks
            parts = _repartition_rows(natives[bi].get("html", ""), [ks for _, ks in run], other, homed)
            for (pg, _ks), html in zip(run, parts):
                if html is None:
                    continue                         # 행 미배정 → 원본 유지(빈 표 방지)
                pages[pg][1]["elements"][idx_of[pg]]["content"] = html
                changed.add(pg)
    for pg in changed:
        j, data = pages[pg]
        try:
            with open(j, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception:
            pass
