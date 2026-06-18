#!/usr/bin/env python3
"""
hwp_figures.py — HWP5/HWPX 임베디드 이미지(시각자료) 위치-인식 salvage.

페이지 렌더(H2Orestart)가 글상자/프레임 이미지를 잘라먹어도, BinData 원본은 온전하다.
이 모듈은 본문을 reading-order 로 훑어 각 이미지가 '어느 섹션·몇 번째 문단'에 있는지(위치),
표시 크기, 원본 BinData 바이트를 함께 추출한다 (페이지 번호는 HWP가 저장하지 않으므로 미제공).

extract_figures(path) -> [ {order, section, para_index, ref, href, media_type,
                            w_in, h_in, context, ext, data} ... ]  (reading-order)
significant(figs, ...) -> 로고/아이콘/얇은 배너를 거른 유의미 figure 목록 (ref 중복 제거)
"""
import os, re, zipfile, struct, zlib, hashlib
import xml.etree.ElementTree as ET
try:
    import olefile
except ImportError:
    olefile = None

HWPUNIT_PER_INCH = 7200
_RASTER = ("png", "jpg", "jpeg", "bmp", "gif")


def _ln(t):
    return t.split('}')[-1]


def _strip_alt(s):
    """이미지 단락의 alt 텍스트('그림입니다 / 원본 그림의 이름: ...')를 제거해 실제 본문만 남김."""
    keep = []
    for line in s.splitlines():
        l = line.strip()
        if not l or l.startswith("그림입니다") or l.startswith("원본 그림의 이름") or l == "원본":
            continue
        keep.append(l)
    return " ".join(keep).strip()


def _norm(s):
    return re.sub(r"\s+", "", s or "")


def _hwpx_item_map(z):
    m = {}
    try:
        hpf = z.read("Contents/content.hpf").decode("utf-8", "replace")
    except Exception:
        return m
    for it in re.findall(r'<opf:item\b[^>]*>', hpf):
        idm = re.search(r'\bid="([^"]*)"', it)
        hr = re.search(r'\bhref="([^"]*)"', it)
        mt = re.search(r'media-type="([^"]*)"', it)
        if idm and hr:
            m[idm.group(1)] = (hr.group(1), mt.group(1) if mt else "")
    return m


def _hwpx_figures(path):
    out = []
    with zipfile.ZipFile(path) as z:
        items = _hwpx_item_map(z)
        names = set(z.namelist())
        secs = sorted([n for n in names if re.match(r"Contents/section\d+\.xml", n)],
                      key=lambda x: int(re.search(r'\d+', x).group()))
        order = 0
        for si, s in enumerate(secs):
            root = ET.fromstring(z.read(s))
            last_anchor = ""
            for pi, p in enumerate(pp for pp in root.iter() if _ln(pp.tag) == "p"):
                ptext = "".join(p.itertext()).strip()
                body = _strip_alt(ptext)
                for pic in (e for e in p.iter() if _ln(e.tag) == "pic"):
                    ref = None
                    cw = ch = 0
                    for el in pic.iter():
                        for k, v in el.attrib.items():
                            if k.split('}')[-1] == "binaryItemIDRef":
                                ref = v
                        if _ln(el.tag) == "curSz":
                            cw = int(el.get("width", 0) or 0)
                            ch = int(el.get("height", 0) or 0)
                    if not ref:
                        continue
                    href, mtype = items.get(ref, (None, ""))
                    if not href:
                        cand = [n for n in names if n.startswith("BinData/")
                                and os.path.splitext(os.path.basename(n))[0] == ref]
                        href = cand[0] if cand else None
                    if not href or href not in names:
                        continue
                    ext = os.path.splitext(href)[1].lstrip(".").lower()
                    if ext not in _RASTER:
                        continue
                    order += 1
                    out.append({
                        "order": order, "section": si, "para_index": pi,
                        "ref": ref, "href": href, "media_type": mtype,
                        "w_in": round(cw / HWPUNIT_PER_INCH, 2),
                        "h_in": round(ch / HWPUNIT_PER_INCH, 2),
                        "context": ptext[:120], "anchor": last_anchor[:80], "ext": ext,
                        "data": z.read(href),
                    })
                if len(_norm(body)) >= 8:
                    last_anchor = body
    return out


def extract_figures(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".hwpx":
        return _hwpx_figures(path)
    if ext == ".hwp":
        return _hwp5_figures(path)
    return []


def _parse_records(buf):
    recs = []; i = 0; n = len(buf)
    while i + 4 <= n:
        h = struct.unpack('<I', buf[i:i + 4])[0]; i += 4
        tag = h & 0x3FF; level = (h >> 10) & 0x3FF; size = (h >> 20) & 0xFFF
        if size == 0xFFF:
            if i + 4 > n: break
            size = struct.unpack('<I', buf[i:i + 4])[0]; i += 4
        recs.append((tag, level, buf[i:i + size])); i += size
    return recs


def _maybe_decompress(raw):
    if raw[:2] == b'\xff\xd8' or raw[:4] == b'\x89PNG' or raw[:2] == b'BM' or raw[:4] == b'GIF8':
        return raw
    for w in (-15, 15):
        try:
            return zlib.decompress(raw, w)
        except Exception:
            pass
    return raw


def _binid_offset(pic_recs, nbins):
    """SHAPE_COMPONENT_PICTURE 안의 bin item id(UINT16) 오프셋 검출. 71 우선, 아니면 동적."""
    def ok(off):
        vals = []
        for r in pic_recs:
            if off + 2 > len(r): return None
            vals.append(struct.unpack_from('<H', r, off)[0])
        return vals
    v = ok(71)
    if v and all(1 <= x <= nbins for x in v):
        return 71
    best = None
    for off in range(50, 110):
        vals = ok(off)
        if vals and all(1 <= x <= nbins for x in vals):
            if len(set(vals)) == len(vals):
                return off
            if best is None:
                best = off
    return best


def _hwp5_figures(path):
    if olefile is None or not olefile.isOleFile(path):
        return []
    ole = olefile.OleFileIO(path)
    try:
        hdr = ole.openstream('FileHeader').read()
        if len(hdr) > 36 and (hdr[36] & 2):
            return []  # encrypted
        comp = bool(hdr[36] & 1)

        def read(stream):
            d = ole.openstream(stream).read()
            return zlib.decompress(d, -15) if comp else d

        # DocInfo BIN_DATA: 순서대로 (stored_id, ext)
        bins = []
        for tag, lvl, rec in _parse_records(read('DocInfo')):
            if tag != 18:
                continue
            try:
                prop = struct.unpack_from('<H', rec, 0)[0]
                btype = prop & 0x0F
                if btype in (1, 2):  # embedding / storage
                    sid = struct.unpack_from('<H', rec, 2)[0]
                    extlen = struct.unpack_from('<H', rec, 4)[0]
                    ext = rec[6:6 + extlen * 2].decode('utf-16-le', 'replace')
                    bins.append((sid, ext.lower()))
                else:
                    bins.append(None)  # link — keep index alignment
            except Exception:
                bins.append(None)
        if not bins:
            return []

        # 실제 BinData 스트림 이름 맵 (대소문자/경로 무관 매칭)
        avail = {}
        for e in ole.listdir():
            if len(e) == 2 and e[0] == 'BinData':
                avail[e[1].upper()] = '/'.join(e)

        secs = sorted([e for e in ole.listdir() if len(e) == 2 and e[0] == 'BodyText'],
                      key=lambda e: int(re.sub(r'\D', '', e[1]) or 0))
        try:
            from hwp_extract import _decode_para_text
        except Exception:
            _decode_para_text = None
        out = []; order = 0
        for si, e in enumerate(secs):
            recs = _parse_records(read('/'.join(e)))
            pic_recs = [rec for tag, lvl, rec in recs if tag == 85]
            off = _binid_offset(pic_recs, len(bins))
            para = -1; last_anchor = ""
            for tag, lvl, rec in recs:
                if tag == 66:
                    para += 1
                elif tag == 67 and _decode_para_text:
                    try:
                        t = _strip_alt(_decode_para_text(rec))
                    except Exception:
                        t = ""
                    if len(_norm(t)) >= 8:
                        last_anchor = t
                if tag != 85:
                    continue
                try:
                    w, h = struct.unpack_from('<ii', rec, 28)
                except Exception:
                    w = h = 0
                bid = None
                if off is not None and off + 2 <= len(rec):
                    bid = struct.unpack_from('<H', rec, off)[0]
                if not bid or bid < 1 or bid > len(bins) or bins[bid - 1] is None:
                    continue
                sid, ext = bins[bid - 1]
                if ext not in _RASTER:
                    continue
                key = f"BIN{sid:04X}.{ext}".upper()
                sname = avail.get(key) or next((v for k, v in avail.items()
                                                if k.startswith(f"BIN{sid:04X}")), None)
                if not sname:
                    continue
                try:
                    data = _maybe_decompress(ole.openstream(sname).read())
                except Exception:
                    continue
                order += 1
                out.append({
                    "order": order, "section": si, "para_index": max(para, 0),
                    "ref": f"BIN{sid:04X}", "href": sname, "media_type": f"image/{ext}",
                    "w_in": round(w / HWPUNIT_PER_INCH, 2),
                    "h_in": round(h / HWPUNIT_PER_INCH, 2),
                    "context": "", "anchor": last_anchor[:80], "ext": ext, "data": data,
                })
        return out
    finally:
        ole.close()


def _word_overlap(a, b):
    """두 문자열의 단어(2자+) 토큰 겹침 계수(교집합/작은쪽). 의역 vs OCR 비교용."""
    A = set(re.findall(r"[가-힣A-Za-z0-9]{2,}", a or ""))
    B = set(re.findall(r"[가-힣A-Za-z0-9]{2,}", b or ""))
    if not A or not B:
        return 0.0
    return len(A & B) / min(len(A), len(B))


def insert_figures_into_pages(doc_dir, fig_records, dedup_vlm=True, dup_thresh=0.3):
    """salvage figure 요소를 앵커 텍스트가 있는 페이지 structured.json 의 해당 위치에 삽입.
    dedup_vlm=True 면, 같은 임베디드 이미지를 VLM 이 페이지에서 읽어 만든 figure 요소(중복)를
    제거한다 — salvage 와 단어 겹침이 큰 VLM figure 만 제거(벡터 도식 등 안 겹치는 건 보존).
    청킹 전에 호출."""
    import glob, json
    pages = sorted(glob.glob(os.path.join(doc_dir, "page_*_structured.json")))
    if not pages:
        return 0
    ptext = {}
    for pj in pages:
        stem = os.path.basename(pj)[:-len("_structured.json")]
        tp = os.path.join(doc_dir, stem + ".txt")
        ptext[pj] = _norm(open(tp, encoding="utf-8").read()) if os.path.exists(tp) else ""

    by_page = {}
    for rec in fig_records:
        anchor = _norm(rec.get("anchor", ""))[:40]
        target = None
        if len(anchor) >= 8:
            for pj in pages:
                if anchor in ptext[pj]:
                    target = pj; break
        if target is None:
            target = pages[-1]
        by_page.setdefault(target, []).append((anchor, rec))

    placed = 0
    for pj, items in by_page.items():
        try:
            data = json.load(open(pj, encoding="utf-8"))
        except Exception:
            continue
        els = data.setdefault("elements", [])
        sal_descs = [r["element"].get("description", "") for _, r in items]
        if dedup_vlm:
            kept = []
            for e in els:
                if e.get("type") == "figure" and not e.get("salvaged"):
                    vt = (e.get("content", "") or "") + " " + (e.get("description", "") or "")
                    if any(_word_overlap(vt, sd) >= dup_thresh for sd in sal_descs):
                        continue   # salvage 와 중복되는 VLM figure 제거
                kept.append(e)
            els = kept
        for anchor, rec in items:
            pos = len(els)
            if len(anchor) >= 8:
                key = anchor[:20]
                for i, el in enumerate(els):
                    if key in _norm(el.get("content", "")):
                        pos = i + 1; break
            els.insert(pos, rec["element"]); placed += 1
        data["elements"] = els
        json.dump(data, open(pj, "w", encoding="utf-8"), ensure_ascii=False, indent=4)
    return placed


def significant(figs, min_dim_in=0.25, max_aspect=18.0, dedup=True):
    """극소 장식(불릿/아이콘)·구분선만 최소한으로 거르고 동일 이미지 중복만 제거한다.
    로고 vs 실제 시각자료 판별은 룰이 아니라 VLM(describe_image)에 맡긴다 —
    작은 진짜 figure(작은 차트·도장·서명 등)를 크기 룰로 버리지 않기 위함."""
    seen = set()
    out = []
    for f in figs:
        w, h = f["w_in"], f["h_in"]
        if min(w, h) < min_dim_in:                       # 불릿/아이콘 등 극소 장식
            continue
        if min(w, h) > 0 and max(w, h) / min(w, h) > max_aspect:
            continue                                      # 구분선/룰 같은 극단 비율
        if dedup:
            hsh = hashlib.md5(f["data"]).hexdigest()
            if hsh in seen:
                continue
            seen.add(hsh)
        out.append(f)
    return out


if __name__ == "__main__":
    import sys
    figs = extract_figures(sys.argv[1])
    sig = significant(figs)
    print(f"전체 figure {len(figs)}개, 유의미 {len(sig)}개")
    for f in sig:
        print(f"  order={f['order']:<3} sec{f['section']} para#{f['para_index']:<4} "
              f"{f['ref']:<9} {f['w_in']}x{f['h_in']}in  ctx='{f['context'][:40]}'")
