"""표 HTML 검증·수리: side-by-side 분할, column-bleed 병합, ragged 행 패딩.
수리 불가 시 needs_retry=True. 의존: bs4 + statistics."""
import re
import statistics
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
                out.append(f"<td>{v}</td>")
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

def validate_and_repair_table(html, caption=None):
    """반환 (elements:list[{'content':html,'caption':...}], needs_retry:bool, issues:list).
    elements 가 2개면 분할 결과."""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        tables = soup.find_all("table")
        if not tables:
            return [{"content": html, "caption": caption}], False, []
        top = tables[0]
        if top.find("table"):                 # 중첩표는 통과
            return [{"content": html, "caption": caption}], False, ["nested_table"]
        rows = _rows_of(top)
        if len(rows) < 2:
            return [{"content": html, "caption": caption}], False, []
        grid, row_width, R, C = _build_grid(rows)
        if C < 2:
            return [{"content": html, "caption": caption}], False, []

        # 병합셀(colspan/rowspan>1) 표는 수리 제외(원본 유지). 무병합 표만 아래 수리.
        has_merge = any(
            (int(c.get("colspan", 1) or 1) > 1 or int(c.get("rowspan", 1) or 1) > 1)
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
                        split_at = ("gutter", c, c + 1); issues.append(("gutter", c, 0)); break
            # (4b) tiled header: 헤더행 좌반복==우반복
            if split_at is None and R >= 2:
                hdr = [_norm(grid[0].get(c, "")) for c in range(C)]
                for k in range(2, C // 2 + 1):
                    if hdr[0:k] == hdr[k:2 * k] and any(hdr[0:k]):
                        if 2 * k == C and not _has_colspan_crossing(grid, k):
                            split_at = ("tiled", k, k); issues.append(("tiled_header", k, 0)); break

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
                return c2 >= 2 and nonempty >= 2
            if _ok(left) and _ok(right):
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
    for nt in native_list or []:
        h = (nt.get("html") or "").strip()
        cs = _cell_set(h)
        if len(cs) >= 2:
            out.append({"html": h, "_set": cs, "_n": len(cs)})
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
    """VLM 조각 본문 첫 셀 키에 해당하는 네이티브 본문행(+rowspan 연속행) + 네이티브 헤더."""
    nh, nb = _split_header_body(native_html)
    _vh, vb = _split_header_body(vlm_html)
    vkeys = {_row_key(tr) for tr in vb if _row_key(tr)}
    if not vkeys:
        return None
    keep_idx = set()
    for i, tr in enumerate(nb):
        if _row_key(tr) in vkeys:
            keep_idx.add(i)
            span = max((_rspan(c) for c in tr.find_all(["td", "th"], recursive=False)), default=1)
            for j in range(i + 1, min(i + span, len(nb))):   # rowspan 그룹 연속행 동반(dangling rowspan 방지)
                cells_j = nb[j].find_all(["td", "th"], recursive=False)
                if cells_j and _rspan(cells_j[0]) > 1:       # 다음 그룹 앵커 → 과다 rowspan 확장 중단
                    break
                keep_idx.add(j)
    keep = [tr for i, tr in enumerate(nb) if i in keep_idx]
    if len(keep) < max(2, len(vb) // 2):     # 키 매칭 빈약 시 포기
        return None
    return "<table>" + "".join(str(tr) for tr in nh) + "".join(str(tr) for tr in keep) + "</table>"

def native_substitute(vlm_html, native_prepared, min_score=0.6):
    """내용 일치하는 네이티브 표 HTML 반환(없으면 None).
    셀집합 포함도(교집합/VLM셀수) 임계 이상, 네이티브가 훨씬 크면 슬라이스."""
    if not native_prepared:
        return None
    # 중첩표(표 안의 표)는 치환 제외(원본 유지).
    try:
        _t = BeautifulSoup(vlm_html or "", "html.parser").find("table")
        if _t is not None and _t.find("table") is not None:
            return None
    except Exception:
        pass
    vset = _cell_set(vlm_html)
    if len(vset) < 4:
        return None
    best, best_score = None, 0.0
    for nt in native_prepared:
        score = len(vset & nt["_set"]) / len(vset)
        if score > best_score:
            best, best_score = nt, score
    if best is None or best_score < min_score:
        return None
    if best["_n"] > len(vset) * 1.5:          # 분할표 → 슬라이스
        return _slice_native(best["html"], vlm_html)
    return best["html"]


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

def _repartition_rows(native_html, page_keysets):
    """native 행 그룹을 페이지 키집합에 1회씩 배정 → 페이지별 표 HTML(미배정 페이지는 None)."""
    nh, nb = _split_header_body(native_html)
    assign, last = {}, 0
    for grp in _rowspan_groups(nb):
        pg = None
        for tr in grp:
            k = _row_key(tr)
            if not k:
                continue
            for pi, ks in enumerate(page_keysets):
                if k in ks:
                    pg = pi; break
            if pg is not None:
                break
        if pg is None:
            pg = last                          # 미매칭 → 인접 페이지(contiguity)
        last = pg
        assign.setdefault(pg, []).extend(grp)
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
    pages, by_native = {}, defaultdict(list)
    for j in sorted(glob.glob(os.path.join(glob.escape(doc_output_dir), "page_*_structured.json"))):
        pg = os.path.basename(j).replace("_structured.json", "")
        try:
            data = json.load(open(j, encoding="utf-8"))
        except Exception:
            continue
        pages[pg] = (j, data)
        for k, e in enumerate(data.get("elements", [])):
            if e.get("type") != "table" or not e.get("_native"):
                continue
            vs = _cell_set(e.get("content", ""))
            if not vs:
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
    changed = set()
    for bi, items in by_native.items():
        if len(items) < 2:
            continue
        idx_of = {pg: k for pg, k, _ in items}
        for run in _safe_runs([(pg, ks) for pg, _, ks in items]):
            parts = _repartition_rows(natives[bi].get("html", ""), [ks for _, ks in run])
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
