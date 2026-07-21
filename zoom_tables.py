"""HWP/HWPX 임베디드 raster 표 재추출.

임베디드 이미지를 확대해 VLM 으로 표를 다시 추출하고, VLM 심판이 원본보다 낫다고
판정할 때만 해당 표 요소를 교체한다. 원본 셀 손실이 임계를 넘으면 교체하지 않는다."""
import os, io, re, json, glob, base64, time
from collections import Counter
from difflib import SequenceMatcher

ZOOM_RASTER_TABLES = os.environ.get("ZOOM_RASTER_TABLES", "1") == "1"
ZOOM_MIN_INCH = float(os.environ.get("ZOOM_MIN_INCH", "3.0"))
ZOOM_MIN_WHITE = float(os.environ.get("ZOOM_MIN_WHITE", "0.4"))
ZOOM_UPSCALE = float(os.environ.get("ZOOM_UPSCALE", "2.0"))
ZOOM_MAXW = int(os.environ.get("ZOOM_MAXW", "2400"))
ZOOM_OVERLAP = float(os.environ.get("ZOOM_OVERLAP", "0.55"))
ZOOM_MIN_TEXT_SIMILARITY = min(
    1.0, max(0.98, float(os.environ.get("ZOOM_MIN_TEXT_SIMILARITY", "0.995")))
)
ZOOM_JUDGES = int(os.environ.get("ZOOM_JUDGES", "5"))
ZOOM_WIN_FRAC = float(os.environ.get("ZOOM_WIN_FRAC", "0.8"))
ZOOM_VLM_TIMEOUT = int(os.environ.get("ZOOM_VLM_TIMEOUT", "180"))
ZOOM_VLM_MAX_TOKENS = max(1024, int(os.environ.get("ZOOM_VLM_MAX_TOKENS", "16384")))
ZOOM_JUDGE_TIMEOUT = int(os.environ.get("ZOOM_JUDGE_TIMEOUT", "60"))
ZOOM_JUDGE_MAX_TOKENS = max(128, int(os.environ.get("ZOOM_JUDGE_MAX_TOKENS", "16384")))
ZOOM_MAX_CANDIDATES = max(0, int(os.environ.get("ZOOM_MAX_CANDIDATES", "16")))
ZOOM_DOC_BUDGET_SEC = max(0.0, float(os.environ.get("ZOOM_DOC_BUDGET_SEC", "900")))

_SYS = """You are an expert document parsing AI. Convert the provided image into structured JSON.

[OUTPUT]
Output ONLY valid JSON (no code fences, no commentary): {"page_number": int, "elements": [{"type": "heading_1|heading_2|heading_3|text|table|figure|footnote", "content": "...", "caption": "..."}]}.
Write natural-language strings in the document's language (Korean for a Korean document).
If the image is not primarily a bordered table/grid, output exactly {"page_number":1,"elements":[]}.

[TABLES — STRUCTURE FIDELITY]
- table content = HTML using ONLY <table>,<tr>,<td> (and <br> only inside a cell) — NEVER <th>. colspan/rowspan allowed. Put the title in "caption".
(1) ONE <table> = ONE continuous bordered grid. If grids sit SIDE BY SIDE, or are STACKED TOP/BOTTOM with a separate border/gap and different column structures, output EACH as its OWN separate <table> element. NEVER merge separate grids.
(2) Fix the column count ONCE from the vertical borders; EVERY <tr> must sum (counting colspan) to exactly that count — pad missing cells with <td></td>, never invent or drop columns.
(3) NO column-bleed: assign each printed value to EXACTLY ONE cell. Adjacent <td> must not duplicate text unless genuinely printed twice. Empty cell is <td></td>.
(4) MERGED cells: spanning N columns -> colspan="N"; N rows -> rowspan="N". Reproduce multi-row headers exactly.
(5) NESTED table (table inside a cell): keep it as a nested <table> inside that <td>.
(6) HEADER WIDTH = BODY WIDTH: header cells (counting colspan) MUST sum to the same column count as body rows. Body [category | sub-item | content] (3 cols) where category spans rows -> header MUST carry colspan to cover all columns.
(8) RECTANGULAR-GRID SELF-CHECK for EVERY <table>: expand colspan/rowspan to a 2-D grid and confirm a PERFECT RECTANGLE — every row resolves to the IDENTICAL column count. No all-blank spacer column.
(9) MULTI-LINE CELL: content stacked on several lines inside one bordered cell -> transcribe EVERY line joined by <br>.
- Transcribe ONLY what is visibly printed. Each piece of content appears exactly once."""


def _ncell(s):
    return re.sub(r"\s+", "", (s or "")).lower()


def _cellset(html):
    from bs4 import BeautifulSoup
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return set()
    return {_ncell(c.get_text(" ", strip=True)) for c in soup.find_all(["td", "th"]) if c.get_text(strip=True)}


def _ordered_cell_text(html):
    """Return normalized cell text in document order, retaining repetitions."""
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return ""
    return "".join(
        value
        for cell in soup.find_all(["td", "th"])
        if (value := _ncell(cell.get_text(" ", strip=True)))
    )


def _table_text_preserved(original_html, candidate_html, threshold=None):
    """Require repeated values and their reading order to remain lossless."""
    if threshold is None:
        threshold = ZOOM_MIN_TEXT_SIMILARITY
    original = _ordered_cell_text(original_html)
    candidate = _ordered_cell_text(candidate_html)
    if not original or not candidate:
        return False
    shared = sum((Counter(original) & Counter(candidate)).values())
    recall = shared / len(original)
    precision = shared / len(candidate)
    sequence = SequenceMatcher(
        None, original, candidate, autojunk=False
    ).ratio()
    return recall >= threshold and precision >= threshold and sequence >= threshold


def _lossless_table_assignment(elements, original_indices, candidates):
    """Match every source table to one text-preserving candidate table."""
    if not original_indices or len(original_indices) != len(candidates):
        return None
    options = {}
    for original_index in original_indices:
        original_html = elements[original_index].get("content", "")
        options[original_index] = [
            candidate_index
            for candidate_index, candidate in enumerate(candidates)
            if _rect_ok(candidate.get("content", ""))
            and _table_text_preserved(
                original_html, candidate.get("content", "")
            )
        ]
        if not options[original_index]:
            return None

    ordered = sorted(original_indices, key=lambda index: len(options[index]))
    assigned = {}
    used = set()

    def match(position):
        if position == len(ordered):
            return True
        original_index = ordered[position]
        for candidate_index in options[original_index]:
            if candidate_index in used:
                continue
            assigned[original_index] = candidate_index
            used.add(candidate_index)
            if match(position + 1):
                return True
            used.remove(candidate_index)
            assigned.pop(original_index, None)
        return False

    return dict(assigned) if match(0) else None


def _overlap(a, b):
    return len(a & b) / len(a) if a else 0.0


def _select_unique_table_page(pdata, pages, zoom_cell_set, candidates):
    """Return one lossless source-page match, or None when location is ambiguous."""
    matches = []
    for page_path in pages:
        data = pdata.get(page_path)
        if not data:
            continue
        elements = data.get("elements", [])
        original_indices = [
            index
            for index, element in enumerate(elements)
            if element.get("type") == "table"
            and _overlap(
                _cellset(element.get("content", "")), zoom_cell_set
            )
            >= ZOOM_OVERLAP
        ]
        if not original_indices:
            continue
        assignment = _lossless_table_assignment(
            elements, original_indices, candidates
        )
        if assignment is None:
            continue
        original_cells = set()
        for index in original_indices:
            original_cells |= _cellset(elements[index].get("content", ""))
        coverage = _overlap(original_cells, zoom_cell_set)
        matches.append(
            (coverage, page_path, original_indices, assignment)
        )
    if not matches:
        return None
    best_coverage = max(item[0] for item in matches)
    winners = [
        item for item in matches if abs(item[0] - best_coverage) <= 1e-9
    ]
    if len(winners) != 1:
        return None
    coverage, page_path, original_indices, assignment = winners[0]
    return page_path, original_indices, assignment, coverage


def _white_frac(im):
    im = im.convert("RGB")
    w, h = im.size
    s = im.resize((min(w, 160), min(h, 160)))
    px = list(s.getdata())
    return sum(1 for r, g, b in px if r > 235 and g > 235 and b > 235) / len(px)


def _rect_ok(html):
    """표의 모든 행이 동일 열수로 전개되면 True(직사각)."""
    try:
        import table_validate as tv
        from bs4 import BeautifulSoup
        t = BeautifulSoup(html or "", "html.parser").find("table")
        if t is None:
            return True
        _g, rw, _R, _C = tv._build_grid(tv._rows_of(t))
        return len(set(rw)) <= 1
    except Exception:
        return False


def _client(api_key):
    from openai import OpenAI
    return OpenAI(base_url=os.environ.get("VLM_BASE_URL", "http://localhost:8000/v1"),
                  api_key=api_key or "EMPTY", timeout=ZOOM_VLM_TIMEOUT, max_retries=0)


def _first_json(text):
    t = (text or "").strip()
    for f in ("```json", "```xml", "```"):
        if t.startswith(f):
            t = t[len(f):]
    t = t.strip()
    if t.endswith("```"):
        t = t[:-3]
    try:
        obj, _ = json.JSONDecoder().raw_decode(t.strip())   # 중복출력(런어웨이)은 첫 객체만
        return obj
    except Exception:
        return None


def _vlm_tables(client, model, img_bytes):
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return None
    w, h = im.size
    tw = min(ZOOM_MAXW, int(w * ZOOM_UPSCALE))
    if tw > w:
        im = im.resize((tw, int(h * tw / w)), Image.LANCZOS)
    buf = io.BytesIO(); im.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": _SYS},
                  {"role": "user", "content": [
                      {"type": "text", "text": "Structure this image strictly as JSON."},
                      {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
        temperature=0.0, max_tokens=ZOOM_VLM_MAX_TOKENS, timeout=ZOOM_VLM_TIMEOUT,
        extra_body={"repetition_penalty": 1.05, "no_repeat_ngram_size": 24})
    obj = _first_json(r.choices[0].message.content or "")
    if not obj:
        return None
    return [e for e in obj.get("elements", [])
            if e.get("type") == "table" and (e.get("content", "") or "").count("<td") >= 4]


def _cols(html):
    try:
        import table_validate as tv
        from bs4 import BeautifulSoup
        t = BeautifulSoup(html or "", "html.parser").find("table")
        if t is None:
            return 0
        _g, _rw, _R, C = tv._build_grid(tv._rows_of(t))
        return C
    except Exception:
        return 0


_JUDGE_SYS = """You are a meticulous table-structure QA judge. You are shown a document image and two candidate HTML structurings of the table(s) in it, labeled A and B. Decide which structuring more FAITHFULLY represents the ACTUAL table structure in the image. Check, in order:
(1) two grids that sit SIDE BY SIDE (or are separated by a gap/gutter/blank column/different columns) must be SEPARATE tables — merging them into one is WRONG;
(2) the column count of each table matches the visible vertical borders;
(3) colspan/rowspan for merged header/category cells is correct, and header width = body width (a header narrower than the body is WRONG);
(4) no cell content is missing, duplicated, or bled into the wrong column.
Output ONLY a JSON object: {"verdict": "A" | "B" | "TIE", "reason": "one short sentence"}. Pick TIE only if they are equally faithful."""


def _judge_once(client, model, img_bytes, html_a, html_b):
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception:
        return None
    w, h = im.size
    tw = min(ZOOM_MAXW, int(w * ZOOM_UPSCALE))
    if tw > w:
        im = im.resize((tw, int(h * tw / w)), Image.LANCZOS)
    buf = io.BytesIO(); im.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    user = f"A:\n{html_a}\n\nB:\n{html_b}\n\nWhich structuring (A or B) more faithfully represents the table structure in the image? JSON only."
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": _JUDGE_SYS},
                  {"role": "user", "content": [
                      {"type": "text", "text": user},
                      {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
        temperature=0.0, max_tokens=ZOOM_JUDGE_MAX_TOKENS, timeout=ZOOM_JUDGE_TIMEOUT)
    obj = _first_json(r.choices[0].message.content or "")
    v = (obj or {}).get("verdict", "").strip().upper()[:1]
    return v if v in ("A", "B") else "TIE" if obj else None


def _judge_zoom_wins(client, model, img_bytes, orig_html, zoom_html, n=None):
    """A/B 순서를 교대하며 n회 심판, zoom 득표율이 ZOOM_WIN_FRAC 이상일 때만 True."""
    n = n or ZOOM_JUDGES
    votes = valid = 0
    for k in range(n):
        zoom_is_b = (k % 2 == 0)
        a = orig_html if zoom_is_b else zoom_html
        b = zoom_html if zoom_is_b else orig_html
        try:
            v = _judge_once(client, model, img_bytes, a, b)
        except Exception:
            v = None
        if v in ("A", "B"):
            valid += 1
            if (v == "B") == zoom_is_b:
                votes += 1
    return valid >= max(2, n - 1) and votes >= valid * ZOOM_WIN_FRAC


def zoom_raster_tables(doc_output_dir, source_path, api_key, model_name):
    """임베디드 raster 표를 zoom-pass 로 재추출해 개선분리본으로 교체. 교체 표 수 반환."""
    if not ZOOM_RASTER_TABLES:
        return 0
    ext = os.path.splitext(source_path or "")[1].lower()
    if ext not in (".hwp", ".hwpx"):
        return 0
    try:
        from hwp_figures import extract_figures, significant
        from PIL import Image
    except Exception:
        return 0
    try:
        figs = significant(extract_figures(source_path))
    except Exception:
        return 0
    cands = [f for f in figs if f.get("w_in", 0) >= ZOOM_MIN_INCH and f.get("h_in", 0) >= ZOOM_MIN_INCH]
    cands.sort(key=lambda f: f.get("w_in", 0) * f.get("h_in", 0), reverse=True)
    if ZOOM_MAX_CANDIDATES > 0:
        cands = cands[:ZOOM_MAX_CANDIDATES]
    if not cands:
        return 0
    pages = sorted(glob.glob(os.path.join(doc_output_dir, "page_*_structured.json")))
    if not pages:
        return 0
    pdata = {}
    for pj in pages:
        try:
            pdata[pj] = json.load(open(pj, encoding="utf-8"))
        except Exception:
            pdata[pj] = None

    client = _client(api_key)
    replaced = 0
    deadline = time.monotonic() + ZOOM_DOC_BUDGET_SEC if ZOOM_DOC_BUDGET_SEC > 0 else None
    for f in cands:
        if deadline is not None and time.monotonic() >= deadline:
            break
        try:
            im = Image.open(io.BytesIO(f["data"])).convert("RGB")
        except Exception:
            continue
        if _white_frac(im) < ZOOM_MIN_WHITE:      # 사진/렌더 → 표 아님, 스킵
            continue
        try:
            ztabs = _vlm_tables(client, model_name, f["data"])
        except Exception:
            continue
        if not ztabs:
            continue
        zset = set()
        for z in ztabs:
            zset |= _cellset(z.get("content", ""))
        if len(zset) < 4:
            continue

        selected = _select_unique_table_page(pdata, pages, zset, ztabs)
        if selected is None:
            continue
        pj, match, assignment, _coverage = selected

        els = pdata[pj]["elements"]
        # Keep candidate order aligned with source-table order for both the
        # judge and in-place replacement. This preserves intervening headings
        # and prevents one large candidate from absorbing neighboring tables.
        ordered_zoom = [ztabs[assignment[index]] for index in match]
        # VLM 심판: 이미지 대비 원본구조 vs zoom구조 — zoom 이 다수결로 명확히 나을 때만 교체
        orig_html = "\n".join(els[i].get("content", "") for i in match)
        zoom_html = "\n".join(z.get("content", "") for z in ordered_zoom)
        if not _judge_zoom_wins(client, model_name, f["data"], orig_html, zoom_html):
            continue

        # Replace one-for-one in place so non-table elements retain their order.
        rebuilt = list(els)
        for original_index in match:
            z = ztabs[assignment[original_index]]
            cap = els[original_index].get("caption", "") or z.get("caption", "")
            rebuilt[original_index] = {
                "type": "table",
                "content": z.get("content", ""),
                "caption": cap,
                "_zoom": True,
                "_source": "zoom_table",
                "_confidence": 0.88,
            }
        pdata[pj]["elements"] = rebuilt
        json.dump(pdata[pj], open(pj, "w", encoding="utf-8"), ensure_ascii=False, indent=4)
        replaced += len(match)
    return replaced
