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
import os, re, zipfile, struct, zlib
import xml.etree.ElementTree as ET
try:
    import olefile
except ImportError:
    olefile = None

HWPUNIT_PER_INCH = 7200
_RASTER = ("png", "jpg", "jpeg", "bmp", "gif")


def _ln(t):
    return t.split('}')[-1]


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
            for pi, p in enumerate(pp for pp in root.iter() if _ln(pp.tag) == "p"):
                ptext = "".join(p.itertext()).strip()
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
                        "context": ptext[:120], "ext": ext,
                        "data": z.read(href),
                    })
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
        out = []; order = 0
        for si, e in enumerate(secs):
            recs = _parse_records(read('/'.join(e)))
            pic_recs = [rec for tag, lvl, rec in recs if tag == 85]
            off = _binid_offset(pic_recs, len(bins))
            para = -1
            for tag, lvl, rec in recs:
                if tag == 66:
                    para += 1
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
                    "context": "", "ext": ext, "data": data,
                })
        return out
    finally:
        ole.close()


def significant(figs, min_dim_in=0.6, min_area_in2=0.7, dedup=True):
    """로고/아이콘/얇은 배너 제거 + (선택) ref 중복 제거. reading-order 유지."""
    seen = set()
    out = []
    for f in figs:
        if min(f["w_in"], f["h_in"]) < min_dim_in:
            continue
        if f["w_in"] * f["h_in"] < min_area_in2:
            continue
        if dedup:
            if f["ref"] in seen:
                continue
            seen.add(f["ref"])
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
