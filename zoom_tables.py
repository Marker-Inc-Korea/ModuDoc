"""HWP/HWPX 임베디드 raster 표 재추출.

임베디드 이미지를 확대해 VLM 으로 표를 다시 추출하고, VLM 심판이 원본보다 낫다고
판정할 때만 해당 표 요소를 교체한다. 원본 셀 손실이 임계를 넘으면 교체하지 않는다."""
import os, io, re, json, glob, base64

ZOOM_RASTER_TABLES = os.environ.get("ZOOM_RASTER_TABLES", "1") == "1"
ZOOM_MIN_INCH = float(os.environ.get("ZOOM_MIN_INCH", "3.0"))
ZOOM_MIN_WHITE = float(os.environ.get("ZOOM_MIN_WHITE", "0.4"))
ZOOM_UPSCALE = float(os.environ.get("ZOOM_UPSCALE", "2.0"))
ZOOM_MAXW = int(os.environ.get("ZOOM_MAXW", "2400"))
ZOOM_OVERLAP = float(os.environ.get("ZOOM_OVERLAP", "0.55"))
ZOOM_MAX_LOSS = float(os.environ.get("ZOOM_MAX_LOSS", "0.10"))
ZOOM_JUDGES = int(os.environ.get("ZOOM_JUDGES", "5"))
ZOOM_WIN_FRAC = float(os.environ.get("ZOOM_WIN_FRAC", "0.8"))

_SYS = """You are an expert document parsing AI. Convert the provided image into structured JSON.

[OUTPUT]
Output ONLY valid JSON (no code fences, no commentary): {"page_number": int, "elements": [{"type": "heading_1|heading_2|heading_3|text|table|figure|footnote", "content": "...", "caption": "..."}]}.
Write natural-language strings in the document's language (Korean for a Korean document).

[TABLES — STRUCTURE FIDELITY]
- table content = HTML using ONLY <table>,<tr>,<td> (and <br> only inside a cell) — NEVER <th>. colspan/rowspan allowed. Put the title in "caption".
(1) ONE <table> = ONE continuous bordered grid. If two or more grids sit SIDE BY SIDE or are separated by a gap/ruled gutter/blank column/different column structure, output EACH as its OWN separate <table> element. NEVER merge side-by-side grids, and never put the right grid's columns into the left grid's row.
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


def _overlap(a, b):
    return len(a & b) / len(a) if a else 0.0


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
        return True


def _client(api_key):
    from openai import OpenAI
    return OpenAI(base_url=os.environ.get("VLM_BASE_URL", "http://localhost:8000/v1"),
                  api_key=api_key or "EMPTY", timeout=600, max_retries=0)


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
        temperature=0.0, max_tokens=16384,
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
        temperature=0.0, max_tokens=512)
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
    for f in cands:
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

        # 원본 표 매칭: 모든 페이지에서 zoom 셀과 겹치는 표 element 를 찾아 커버리지 최대 페이지 선택
        best = None
        for pj in pages:
            d = pdata[pj]
            if not d:
                continue
            els = d.get("elements", [])
            match = [i for i, e in enumerate(els)
                     if e.get("type") == "table" and _overlap(_cellset(e.get("content", "")), zset) >= ZOOM_OVERLAP]
            if not match:
                continue
            oset = set()
            for i in match:
                oset |= _cellset(els[i].get("content", ""))
            cov = _overlap(oset, zset)
            if best is None or cov > best[3]:
                best = (pj, match, oset, cov)
        if best is None:
            continue
        pj, match, oset, cov = best

        # 무손실 안전망: 원본 셀 대부분이 zoom 출력에 존재해야(심판 무관, 항상 강제)
        lost = [c for c in oset if c not in zset and len(c) > 1]
        if len(lost) > max(1, len(oset) * ZOOM_MAX_LOSS):
            continue
        els = pdata[pj]["elements"]
        # VLM 심판: 이미지 대비 원본구조 vs zoom구조 — zoom 이 다수결로 명확히 나을 때만 교체
        orig_html = "\n".join(els[i].get("content", "") for i in match)
        zoom_html = "\n".join(z.get("content", "") for z in ztabs)
        if not _judge_zoom_wins(client, model_name, f["data"], orig_html, zoom_html):
            continue

        # 교체: 매칭 표 element 제거, 첫 매칭 위치에 zoom 표 삽입(캡션은 원본 제목 우선)
        mset = set(match); pos = min(match)
        newz = []
        for z in ztabs:
            zc = _cellset(z.get("content", ""))
            bi = max(match, key=lambda i: _overlap(_cellset(els[i].get("content", "")), zc), default=None)
            cap = (els[bi].get("caption", "") if bi is not None else "") or z.get("caption", "")
            newz.append({"type": "table", "content": z.get("content", ""), "caption": cap, "_zoom": True})
        pdata[pj]["elements"] = ([e for i, e in enumerate(els) if i < pos and i not in mset]
                                 + newz
                                 + [e for i, e in enumerate(els) if i > pos and i not in mset])
        json.dump(pdata[pj], open(pj, "w", encoding="utf-8"), ensure_ascii=False, indent=4)
        replaced += len(newz)
    return replaced
