#!/usr/bin/env python3
import zipfile, re, math
import xml.etree.ElementTree as ET

RESET = 15000

def _local(t): return t.split('}')[-1]

def _body_height(root):
    for el in root.iter():
        if _local(el.tag) == "pagePr":
            H = int(el.get("height")); mt = mb = hd = ft = 0
            for c in el:
                if _local(c.tag) == "margin":
                    mt = int(c.get("top", 0));  mb = int(c.get("bottom", 0))
                    hd = int(c.get("header", 0)); ft = int(c.get("footer", 0))
            return H - mt - mb - hd - ft
    return None

def _cellsz_h(tc):
    for c in tc:
        if _local(c.tag) == "cellSz":
            return int(c.get("height", 0))
    return 0

def _rowspan(tc):
    for c in tc:
        if _local(c.tag) == "cellSpan":
            return int(c.get("rowSpan", 1))
    return 1

def _cell_content_h(tc):
    mar = 0
    for c in tc:
        if _local(c.tag) == "cellMargin":
            mar = int(c.get("top", 0)) + int(c.get("bottom", 0))
    h = 0
    for sub in tc:
        if _local(sub.tag) != "subList":
            continue
        for p in sub:
            if _local(p.tag) != "p":
                continue
            for run in p:
                if _local(run.tag) == "linesegarray":
                    for ls in run:
                        if _local(ls.tag) == "lineseg":
                            h += int(ls.get("vertsize", 0))
                elif _local(run.tag) == "run":
                    for x in run:
                        if _local(x.tag) == "tbl":
                            h += _table_height(x)
    return h + mar

def _row_height(tr):
    hmax = 0
    for tc in tr:
        if _local(tc.tag) != "tc":
            continue
        h = max(_cellsz_h(tc), _cell_content_h(tc))
        rs = _rowspan(tc)
        if rs > 1:
            h = h // rs
        hmax = max(hmax, h)
    return max(hmax, 300)

def _table_height(tbl):
    return sum(_row_height(tr) for tr in tbl if _local(tr.tag) == "tr")

class _St:
    __slots__ = ("BH", "pages", "y", "last")
    def __init__(s, BH): s.BH = BH; s.pages = 1; s.y = 0; s.last = None

def _simulate(root):
    BH = _body_height(root)
    if not BH or BH <= 0:
        return None
    st = _St(BH)

    def walk(el, in_tbl):
        tag = _local(el.tag)
        if tag == "tbl":
            if not in_tbl:
                rows = [_row_height(tr) for tr in el if _local(tr.tag) == "tr"]
                hdr = rows[0] if rows else 0
                rep = el.get("repeatHeader", "0") == "1"
                for ri, rh in enumerate(rows):
                    avail = st.BH - st.y
                    if rh > avail and st.y > 0:
                        st.pages += 1; st.y = 0
                        if rep and ri > 0:
                            st.y += hdr
                    cont = max(st.BH - (hdr if rep else 0), 1000)
                    if rh > st.BH:
                        over = rh - (st.BH - st.y)
                        extra = math.ceil(over / cont)
                        st.pages += extra
                        st.y = max(over - (extra - 1) * cont, 0)
                    else:
                        st.y += rh
                st.last = None
            return
        if tag == "lineseg" and not in_tbl:
            vp = int(el.get("vertpos", 0)); vs = int(el.get("vertsize", 0))
            if st.last is not None and vp < st.last - RESET:
                st.pages += 1
            st.last = vp; st.y = vp + vs
            return
        if tag == "p" and not in_tbl and el.get("pageBreak") == "1":
            st.pages += 1; st.y = 0; st.last = None
        for c in el:
            walk(c, in_tbl)

    walk(root, False)
    return st.pages

def derive_page_count(hwpx_path):
    try:
        with zipfile.ZipFile(hwpx_path) as z:
            secs = sorted(n for n in z.namelist()
                          if re.match(r"Contents/section\d+\.xml", n))
            if not secs:
                return None
            total = 0
            for n in secs:
                p = _simulate(ET.fromstring(z.read(n)))
                if p is None:
                    return None
                total += p
            return total
    except Exception:
        return None

if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        print(derive_page_count(p), p)
