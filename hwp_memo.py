"""HWP/HWPX 메모(주석) 텍스트 추출. 페이지 이미지에 렌더되지 않는 편집기 메모를 파일에서 파싱.
방어적(실패 시 []). 외부 의존: zlib/struct + olefile."""
import io
import struct
import zipfile
import logging

logger = logging.getLogger(__name__)

# --- HWPX (OWPML) ---
def _localname(tag):
    return tag.rsplit("}", 1)[-1]

def _hwpx_memos(path):
    from xml.etree import ElementTree as ET
    memos = []
    with zipfile.ZipFile(path) as z:
        parts = sorted(n for n in z.namelist()
                       if n.startswith("Contents/section") and n.endswith(".xml"))
        for n in parts:
            try:
                root = ET.fromstring(z.read(n))
            except Exception:
                continue
            for el in root.iter():
                if _localname(el.tag) != "memo":
                    continue
                parts_txt = [sub.text for sub in el.iter()
                             if _localname(sub.tag) == "t" and sub.text]
                txt = "".join(parts_txt).strip()
                if txt:
                    memos.append(txt)
    return memos

# --- HWP5 (OLE 바이너리) ---
HWPTAG_BEGIN = 0x10
TAG_MEMO_LIST = HWPTAG_BEGIN + 77   # 0x5D
TAG_PARA_TEXT = HWPTAG_BEGIN + 51   # 0x43
_EXT_CTRL = {1, 2, 3, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}  # 16바이트 인라인 컨트롤

def _records(buf):
    f = io.BytesIO(buf)
    while True:
        h = f.read(4)
        if len(h) < 4:
            return
        w = struct.unpack("<I", h)[0]
        tag = w & 0x3FF
        level = (w >> 10) & 0x3FF
        size = (w >> 20) & 0xFFF
        if size == 0xFFF:
            ext = f.read(4)
            if len(ext) < 4:
                return
            size = struct.unpack("<I", ext)[0]
        payload = f.read(size)
        if len(payload) < size:
            return
        yield tag, level, payload

def _decode_para_text(payload):
    out = []
    i, n = 0, len(payload) - 1
    while i < n:
        code = payload[i] | (payload[i + 1] << 8)
        if code < 0x20:
            i += 16 if code in _EXT_CTRL else 2   # 컨트롤 건너뜀
            continue
        out.append(chr(code))
        i += 2
    return "".join(out)

def _hwp5_memos(path):
    import olefile
    import zlib
    memos = []
    ole = olefile.OleFileIO(path)
    try:
        fh = ole.openstream("FileHeader").read()
        flags = struct.unpack("<I", fh[36:40])[0]
        compressed = flags & 1
        if flags & 2 or flags & 4:     # 암호화/배포용 → 스킵
            return memos

        def load(name):
            data = ole.openstream(name).read()
            return zlib.decompress(data, -15) if compressed else data

        n = 0
        while ole.exists(f"BodyText/Section{n}"):
            try:
                buf = load(f"BodyText/Section{n}")
            except Exception:
                n += 1
                continue
            in_memo = None     # 메모 진입 레벨
            cur = []
            for tag, level, payload in _records(buf):
                if tag == TAG_MEMO_LIST:
                    if cur:
                        memos.append("".join(cur).strip()); cur = []
                    in_memo = level
                elif in_memo is not None:
                    if level <= in_memo:        # 메모 서브트리 종료
                        if cur:
                            memos.append("".join(cur).strip()); cur = []
                        in_memo = None
                    elif tag == TAG_PARA_TEXT:
                        cur.append(_decode_para_text(payload))
            if cur:
                memos.append("".join(cur).strip())
            n += 1
    finally:
        ole.close()
    return [m for m in memos if m]

def extract_hwp_memos(path):
    """HWP/HWPX 메모 텍스트 리스트 추출. 실패/없음 시 []."""
    try:
        if zipfile.is_zipfile(path):     # PK = HWPX
            return _hwpx_memos(path)
        return _hwp5_memos(path)         # OLE = HWP5
    except Exception as e:
        logger.warning(f"HWP 메모 추출 실패({path}): {e}")
        return []
