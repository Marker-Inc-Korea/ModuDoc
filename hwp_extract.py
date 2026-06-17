#!/usr/bin/env python3
import os, re, zlib, struct, zipfile
import xml.etree.ElementTree as ET
try:
    import olefile
except ImportError:
    olefile = None

T_PARA_HEADER = 66
T_PARA_TEXT   = 67
T_CTRL_HEADER = 71
T_LIST_HEADER = 72
T_TABLE       = 77

_EXT    = {1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23}
_INLINE = {4, 5, 6, 7, 8, 9, 19, 20}


def _decode_para_text(data):
    out = []; i = 0; n = len(data)
    while i + 1 < n:
        c = data[i] | (data[i + 1] << 8)
        if c in _EXT or c in _INLINE:
            if c == 9: out.append('\t')
            i += 16
        elif c < 32:
            if c in (10, 13): out.append('\n')
            i += 2
        else:
            out.append(chr(c)); i += 2
    return ''.join(out)


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


def _build_tree(recs):
    root = {"tag": -1, "data": b"", "children": []}
    stack = [(-1, root)]
    for tag, level, data in recs:
        node = {"tag": tag, "data": data, "children": []}
        while stack and stack[-1][0] >= level:
            stack.pop()
        if not stack:
            stack = [(-1, root)]
        stack[-1][1]["children"].append(node)
        stack.append((level, node))
    return root


def _ctrl_id(data):
    if len(data) < 4: return ""
    return data[0:4][::-1].decode('ascii', 'replace')


def _cell_pos(list_header_data):
    try:
        col, row, cs, rs = struct.unpack('<HHHH', list_header_data[6:14])
        return col, row, max(cs, 1), max(rs, 1)
    except Exception:
        return None


def _emit_table(ctrl, out):
    out.append("\n<table>")
    last_row = None; open_row = False; open_cell = False
    for ch in ctrl["children"]:
        if ch["tag"] == T_TABLE:
            continue
        if ch["tag"] == T_LIST_HEADER:
            pos = _cell_pos(ch["data"])
            _, row, cs, rs = pos if pos else (0, (last_row or 0), 1, 1)
            if open_cell: out.append("</td>"); open_cell = False
            if row != last_row:
                if open_row: out.append("</tr>")
                out.append("<tr>"); open_row = True; last_row = row
            sp = (f" colspan='{cs}'" if cs > 1 else "") + (f" rowspan='{rs}'" if rs > 1 else "")
            out.append(f"<td{sp}>"); open_cell = True
        else:
            _emit(ch, out)
    if open_cell: out.append("</td>")
    if open_row: out.append("</tr>")
    out.append("</table>\n")


def _emit(node, out):
    for ch in node["children"]:
        tag = ch["tag"]
        if tag == T_PARA_TEXT:
            out.append(_decode_para_text(ch["data"])); out.append("\n")
        elif tag == T_CTRL_HEADER and _ctrl_id(ch["data"]) == "tbl ":
            _emit_table(ch, out)
        else:
            _emit(ch, out)


def extract_hwp5(path):
    if olefile is None:
        return "", "no-olefile"
    if not olefile.isOleFile(path):
        return "", "not-ole"
    ole = olefile.OleFileIO(path)
    try:
        hdr = ole.openstream('FileHeader').read()
        flags = hdr[36] if len(hdr) > 36 else 0
        comp = bool(flags & 1); enc = bool(flags & 2); dist = bool(flags & 4)
        if enc:
            return "", "encrypted"
        secs = sorted([e for e in ole.listdir() if len(e) == 2 and e[0] == 'BodyText'],
                      key=lambda e: int(re.sub(r'\D', '', e[1]) or 0))
        if not secs and dist:
            secs = sorted([e for e in ole.listdir() if len(e) == 2 and e[0] == 'ViewText'],
                          key=lambda e: int(re.sub(r'\D', '', e[1]) or 0))
        out = []
        for e in secs:
            data = ole.openstream(e).read()
            if comp:
                try:
                    data = zlib.decompress(data, -15)
                except Exception:
                    try: data = zlib.decompress(data)
                    except Exception: continue
            _emit(_build_tree(_parse_records(data)), out)
        return "".join(out), "ok"
    finally:
        ole.close()


def _lname(t): return t.split('}')[-1]


def _hwpx_run_text(run):
    parts = []
    for ch in run:
        n = _lname(ch.tag)
        if n == "t":
            parts.append("".join(ch.itertext()))
        elif n == "tab":
            parts.append("\t")
        elif n == "lineBreak":
            parts.append("\n")
        elif n == "tbl":
            parts.append(_hwpx_table(ch))
        elif n in ("pic", "ole", "rect", "ellipse", "line", "polygon", "curve", "container"):
            sub = "".join(_hwpx_para_text(p) for p in ch.iter()
                          if _lname(p.tag) == "p" and p is not ch)
            if sub.strip(): parts.append(sub)
    return "".join(parts)


def _hwpx_para_text(p):
    return "".join(_hwpx_run_text(run) for run in p if _lname(run.tag) == "run")


def _hwpx_table(tbl):
    html = "<table>"
    for tr in tbl:
        if _lname(tr.tag) != "tr": continue
        html += "<tr>"
        for tc in tr:
            if _lname(tc.tag) != "tc": continue
            cs = rs = "1"
            for c in tc:
                if _lname(c.tag) == "cellSpan":
                    cs = c.get("colSpan", "1"); rs = c.get("rowSpan", "1")
            content = []
            for sub in tc:
                if _lname(sub.tag) == "subList":
                    for p in sub:
                        if _lname(p.tag) == "p":
                            content.append(_hwpx_para_text(p))
            sp = (f" colspan='{cs}'" if cs != '1' else "") + (f" rowspan='{rs}'" if rs != '1' else "")
            html += f"<td{sp}>{' '.join(x.strip() for x in content if x.strip())}</td>"
        html += "</tr>"
    return html + "</table>"


def extract_hwpx(path):
    try:
        out = []
        with zipfile.ZipFile(path) as z:
            secs = sorted([n for n in z.namelist() if re.match(r"Contents/section\d+\.xml", n)],
                          key=lambda x: int(re.search(r'\d+', x).group()))
            for s in secs:
                root = ET.fromstring(z.read(s))
                for p in root:
                    if _lname(p.tag) != "p": continue
                    t = _hwpx_para_text(p)
                    if t.strip(): out.append(t.strip())
        return "\n".join(out), "ok"
    except Exception as e:
        return "", f"error:{e}"


def extract(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".hwpx": return extract_hwpx(path)
    if ext == ".hwp":  return extract_hwp5(path)
    return "", "unsupported"
