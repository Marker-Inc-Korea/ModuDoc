import os
import json
import re
import logging

logger = logging.getLogger(__name__)

# VLM 은 heading_1/2/3 만 내보내지만, 정규화 단계에서 깊은 십진구조(7.6.1.1 등)를
# 보존하려고 heading_4~6 까지 레벨을 부여할 수 있다(빌더가 그대로 처리).
HEADING_LEVEL = {f"heading_{i}": i for i in range(1, 7)}
HEADING_TYPES = set(HEADING_LEVEL)
MAX_HEADING_LEVEL = 6

# 한국 공문서/규정 번호체계 (상위 → 하위). toc/tree 청킹 전, VLM 이 페이지별로 매긴
# heading_1/2/3 레벨을 이 체계로 문서 전역에서 일관되게 재부여한다(복잡 문서 레벨 흔들림 보정).
_HANGUL_ORD = "가나다라마바사아자차카타파하"
_NUM_PATTERNS = [
    ("pyeon", re.compile(r'^제\s*\d+\s*[편부]')),
    ("jang",  re.compile(r'^(?:제\s*\d+\s*장|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[.\s])')),
    ("roman", re.compile(r'^[IVXLCDM]{1,7}\.\s')),     # ASCII 로마자 최상위 섹션(I. II. … VIII.)
    ("jeol",  re.compile(r'^제\s*\d+\s*절')),
    ("gwan",  re.compile(r'^제\s*\d+\s*관')),
    ("jo",    re.compile(r'^제\s*\d+\s*조(?:의\s*\d+)?')),
    ("hang",  re.compile(r'^[①-⑳]')),                 # ①~⑳
    ("ho",    re.compile(r'^\d+\.(?:\s|$)')),                   # 1.
    ("mok",   re.compile(rf'^[{_HANGUL_ORD}]\.(?:\s|$)')),      # 가.
    ("pnum",  re.compile(r'^\(\s*\d+\s*\)')),                   # (1)
    ("pga",   re.compile(rf'^\(\s*[{_HANGUL_ORD}]\s*\)')),      # (가)
    ("nump",  re.compile(r'^\d+\)')),                           # 1)
    ("gap",   re.compile(rf'^[{_HANGUL_ORD}]\)')),             # 가)
    ("sq",    re.compile(r'^[□■]\s')),                          # □
    ("circ",  re.compile(r'^[○◦●]\s')),                         # ○
    ("dash",  re.compile(r'^[-–·∙]\s')),                        # -
]
_PATTERN_RANK = {name: i for i, (name, _) in enumerate(_NUM_PATTERNS)}
_STRONG = {"pyeon", "jang", "roman", "jeol", "gwan", "jo"}
MAX_CHUNK_CHARS = int(os.environ.get("CHUNK_MAX_CHARS", "4000"))
# 크기 분할로 생긴 하위청크 사이 오버랩(자). 0=비활성. (크기 분할에만 적용)
CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", "0"))


def _detect_pattern(text: str):
    t = (text or "").lstrip()
    for name, rx in _NUM_PATTERNS:
        if rx.match(t):
            return name
    return None


_ANCHOR_KW = ("별표", "별지", "부칙", "서식", "신구조문", "신·구조문", "신 · 구조문", "구조문대비표",
              "붙임", "첨부", "참고")   # 붙임/참고/첨부 = 본문 섹션과 형제인 최상위 첨부 블록
# 앵커 매칭은 구분자(가운뎃점·마침표·반각중점)·공백 무시.
_ANCHOR_SEP_RX = re.compile(r"[·.ㆍ・∙•\s]")
def _anchor_norm(s: str) -> str:
    return _ANCHOR_SEP_RX.sub("", s or "")
_ANCHOR_KW_N = tuple(_anchor_norm(k) for k in _ANCHOR_KW)
# 끝점 십진도 허용: '7.6' / '7.6.' / '7.6.1' / '7.6.1.' 모두 인식(끝 '.' 는 캡처 밖).
_DECIMAL_RX = re.compile(r'^(\d+(?:\.\d+)+)\.?(?:\s|$|[^\d.])')
_BYLAW_REF_RX = re.compile(r'\(\s*제\s*\d+\s*조[^)]*관련\s*\)')   # '...(제5조 관련)' 별표 제목
# 마커별 절대 랭크(작을수록 상위). 같은 랭크 = 형제(같은 레벨). 십진 다단계는 점 개수로.
# 한국 번호체계 계층: 조 > ①항 > 1.호(+십진) > 가.목 > (1) > (가) > 1) > 가) > □ > ○ > -
_RANK = {
    "pyeon": 0, "jang": 2, "roman": 2, "jeol": 4, "gwan": 6, "jo": 8,
    "hang": 20, "ho": 30, "mok": 40,           # 십진 다단계는 _heading_rank 에서 31~35
    "pnum": 50, "pga": 52, "nump": 60, "gap": 62,
    "sq": 70, "circ": 74, "dash": 78,
}
# 마커없는 heading 은 번호마커보다 약하게(90+) 둔다. h1<h2<h3 순서 유지.
# 별표/별지/부칙·'(제N조 관련)' 참조·문서 첫 heading 은 rank 1(최상위 섹션).
_VLM_RANK = {"heading_1": 90, "heading_2": 92, "heading_3": 94}
_JO_TITLE_RX = re.compile(r'^(제\s*\d+\s*조(?:의\s*\d+)?\s*(?:\([^)]*\))?)')
_BU_TITLE_RX = re.compile(r'^(제\s*\d+\s*[편장절관부](?:\s*\([^)]*\))?)')


def _heading_rank(content: str, vlm_type: str) -> int:
    """heading 의 계층 랭크(작을수록 상위). 같은 랭크는 형제로 처리된다."""
    c = (content or "").strip()
    if any(_anchor_norm(c.lstrip("[ \t")).startswith(k) for k in _ANCHOR_KW_N) or _BYLAW_REF_RX.search(c):
        return 1                                       # 별표/별지/부칙·'(제N조 관련)' = 최상위 섹션
    m = _DECIMAL_RX.match(c)
    if m:
        return 30 + min(m.group(1).count("."), 5)     # 4.1→31, 4.1.1→32, 7.6.→31 ...
    pat = _detect_pattern(c)
    if pat in _RANK:
        return _RANK[pat]
    return _VLM_RANK.get(vlm_type, 94)


# 정의 항목: 'N. "용어"(이)란/은/는 …' — 인용 용어가 곧 짧은 제목이 된다.
# 복합 용어('N. "A" 또는 "B"란 …')도 잡도록 마지막 인용부호까지 허용(비탐욕).
_DEF_ENTRY_RX = re.compile(r'^\s*\d+\.\s*["“”].{1,200}?["“”]\s*(?:이?란|이라(?:고)?|은|는)\b')
_DEF_TITLE_RX = re.compile(r'^\s*(\d+\.\s*["“”][^"“”\n]{1,80}["“”])')


def _is_def_entry(content: str) -> bool:
    """정의 리스트 항목인가('N. "용어"란 …')."""
    return bool(_DEF_ENTRY_RX.match(content or ""))


# 절/리스트 마커. 항(①)은 항상, 그 외는 '긴 문장'일 때만 본문으로 강등.
_CLAUSE_MARKERS = {"hang", "ho", "mok", "pnum", "pga", "nump", "gap", "sq", "circ", "dash"}
CLAUSE_HEADING_MAX = int(os.environ.get("CHUNK_CLAUSE_HEADING_MAX", "40"))


def _is_clause_body(content: str) -> bool:
    """절 마커 heading 이 실은 본문(긴 문장)인가. 항(①)은 항상, 그 외는 길면 True.
    정의 항목('N."용어"')과 짧은 제목('가. 도안')은 제외."""
    if _is_def_entry(content):
        return False
    pat = _detect_pattern(content)
    if pat not in _CLAUSE_MARKERS:
        return False
    return pat == "hang" or len((content or "").strip()) > CLAUSE_HEADING_MAX


_PAGE_FOOTER_RX = re.compile(r"\s*-\s*[\d\s]{1,7}-\s*$")   # 말미 페이지번호 꼬리말('- 21 -', 자간 '- 2 1 -')

def _clean_heading_title(content: str) -> str:
    """heading 의 짧은 제목만 추출(잘린 제목/본문혼입 방지). 제N조(제목) 또는 정의 'N."용어"'."""
    c = (content or "").strip()
    c2 = _PAGE_FOOTER_RX.sub("", c).strip()               # 러닝헤더에 붙은 페이지번호 furniture 제거
    if c2:
        c = c2
    m = _DEF_TITLE_RX.match(c)
    if m and len(m.group(1)) < len(c):
        return m.group(1).strip()                  # 'N. "용어"' (정의 항목)
    for rx in (_JO_TITLE_RX, _BU_TITLE_RX):
        m = rx.match(c)
        if m and m.group(1).strip() and len(m.group(1)) < len(c):
            return m.group(1).strip()
    return c


def _promotable(e: dict) -> bool:
    """VLM 이 text 로 둔 진짜 조/장 시작만 heading 으로 승급(상호참조·표셀 오승급 차단).
    진짜 조문의 신뢰 신호는 '제N조(제목)' 의 괄호다. 본문에 '에 따라/참조' 가 들어있어도
    조문이면 승급한다 — 본문을 스캔해 조문을 통째 누락시키던 거짓음성을 제거."""
    if e.get("type") not in ("text", "paragraph"):
        return False
    c = (e.get("content", "") or "").strip()
    if _detect_pattern(c) not in _STRONG:
        return False
    m = re.match(r'^(제\s*\d+\s*조(?:의\s*\d+)?|제\s*\d+\s*[편장절관부])', c)
    if not m:
        return False
    rest = c[m.end():].lstrip()
    if rest[:1] == "(":                                # 제N조(제목) … = 진짜 조문 시작
        return True
    if rest[:1] == "제":                               # 제4조제1항… = 상호참조
        return False
    if re.search(r'(참조|준용)', rest[:20]):           # '제8조 참조' 같은 짧은 참조
        return False
    return len(c) <= 25                                # 괄호 없는 짧은 제목만


_TERMINATOR = re.compile(r'(?:[.!?:;)\]}…」』”’"\']|\d|[다음함임됨등것호점율도년월일])\s*$')
# 구/문장을 끝낼 수 없는 연결어미·조사 — 이걸로 끝나면 다음 줄로 잘렸다는 강한 신호.
_INCOMPLETE_END = re.compile(
    r'(?:대[한해]|위[한해]|관[한해]|따[라른]|및|와|과|의|에|를|을|로|으로|에게|에서|하여|하고|되어|또는)\s*$')


def _looks_complete(s: str) -> bool:
    """문장/구가 종결돼 보이는가(페이지 경계 잘림 판별용)."""
    s = (s or "").rstrip()
    return (not s) or bool(_TERMINATOR.search(s))


def _prev_incomplete(prev: dict) -> bool:
    """직전 element 가 페이지 경계에서 잘려 보이는가(다음 heading 이 그 꼬리일 가능성).
    연결어미/조사로 끝날 때만 '잘림' 으로 본다 — 명사로 끝나는 정상 제목/서명줄
    ('…식품의약품안전처장')을 미완결로 오판해 제목을 강등하던 거짓양성 방지."""
    if prev is None or prev.get("type") not in ("text", "paragraph", *HEADING_TYPES):
        return False
    return bool(_INCOMPLETE_END.search((prev.get("content", "") or "").rstrip()))


def _normalize_heading_levels(elements: list) -> list:
    """최상위 섹션 이월(carry-forward) 기반 heading 레벨 재부여.
    하나의 스택으로 마커 절대랭크(_heading_rank)에 따라 깊이를 매기되 **같은 랭크는 항상
    형제(같은 레벨)** — 십진 형제(1./2., 4.1.1/4.1.2)가 부모-자식으로 잘못 묶이던 버그를
    구조적으로 차단한다. 상위 섹션(제N조/장/별표/마커없는 heading_1)은 다음 섹션이 나올
    때까지 스택에 남아 모든 하위 청크에 이월된다. 레벨은 heading_6 까지 캡(깊이점프 0 보장).
    본문(text)에 묻힌 진짜 조/장 시작만 보수적으로 승급(_promotable)하고, 페이지 경계에서
    잘린 단어/문장 조각이 heading 으로 오라벨된 경우는 본문으로 강등한다. 제목은 정리한다.
    CHUNK_NORMALIZE=0 으로 비활성(보정 전/후 비교용)."""
    if os.environ.get("CHUNK_NORMALIZE", "1") == "0":
        return elements

    stack = []   # 열린 heading 들의 랭크(상위→하위 spine)
    first = True
    prev = None
    for e in elements:
        if e.get("type") not in HEADING_TYPES:
            if _promotable(e):                         # 제N조/장 = 본문에 묻힌 진짜 조문
                e["type"] = "heading_1"
                e["_promoted_heading"] = True
            elif _is_def_entry(e.get("content", "") or ""):   # 'N."용어"란 …' 정의 항목
                e["type"] = "heading_2"
                e["_promoted_heading"] = True
        content = e.get("content", "") or ""
        # 잘린 조각이 heading 으로 오라벨된 경우 → 본문 강등.
        # 마커없는 heading(rank>=90) + 직전이 미완결일 때만(보수적).
        if (e.get("type") in HEADING_TYPES and not first
                and _heading_rank(content, e.get("type")) >= 90
                and _prev_incomplete(prev)):
            e["type"] = "text"
            e["_torn_fragment"] = True
        # 절/리스트 마커(항·긴 호/목)가 heading 으로 오라벨된 경우 → 본문으로 강등.
        if e.get("type") in HEADING_TYPES and _is_clause_body(content):
            e["type"] = "text"
            e["_clause_body"] = True
        if e.get("type") not in HEADING_TYPES:
            prev = e
            continue
        r = _heading_rank(content, e.get("type"))
        # 문서 첫 heading 이 마커없는 제목(예: 문서 제목)이면 최상위 루트로 본다.
        if first and _detect_pattern(content) is None and r >= 10:
            r = 1
        # 명시적 최상위 섹션(예: XLSX 시트명) — 항상 루트로
        if e.get("_section_root"):
            r = 1
        first = False
        while stack and stack[-1] >= r:
            stack.pop()
        level = min(len(stack) + 1, MAX_HEADING_LEVEL)
        stack.append(r)
        e["type"] = f"heading_{level}"
        title = _clean_heading_title(content)
        if title != content.strip():
            e["heading_title"] = title
        prev = e
    return elements


# 반복 머리글의 '(계속)/(continued)' 연속표시 — 괄호 안에 있을 때만(영어 본문의 'continued'
# 같은 일반 단어를 연속표시로 오인해 heading 을 삭제하던 거짓양성 방지).
_CONT_MARK = re.compile(r'\(\s*(?:계\s*속|이어서|이어짐|cont(?:inued|[\'’]?d)?)\s*\)', re.IGNORECASE)


def _norm_title(s: str) -> str:
    """heading 제목 비교용 정규화: 선행 번호마커 제거 + 공백 1칸 정규화 + 소문자.
    내부 공백은 보존한다('상호 관계' ≠ '상호관계') — 다른 제목을 같은 반복으로 오인해
    삭제하던 거짓양성 병합 방지."""
    s = (s or "").strip()
    pat = _detect_pattern(s)
    if pat:
        s = dict(_NUM_PATTERNS)[pat].sub("", s, count=1)
    return re.sub(r'\s+', ' ', s).strip().lower()


def _merge_continued_headings(elements: list) -> list:
    """페이지를 넘어가며 반복된 머리글/'(계속)' 표시로 VLM 이 잘못 만든 heading 을 제거해,
    한 섹션이 엉뚱하게 쪼개지는 것을 막는다(다음 페이지로 이어진 내용을 직전 섹션에 병합).
    CHUNK_MERGE_CONTINUED=0 으로 비활성(비교용)."""
    if os.environ.get("CHUNK_MERGE_CONTINUED", "1") == "0":
        return elements
    out, open_stack, prev_page = [], [], None
    for e in elements:
        pg = e.get("page_number")
        if e.get("type") in HEADING_TYPES:
            content = e.get("content", "") or ""
            lvl = HEADING_LEVEL[e["type"]]
            norm = _norm_title(content)
            page_changed = prev_page is not None and pg is not None and pg != prev_page
            # (a) 명시적 연속 표시('(계속)' 등) → 가짜 heading 제거
            if _CONT_MARK.search(content):
                prev_page = pg if pg is not None else prev_page
                continue
            # (b) 페이지 전환 직후 열린 heading 과 동일 제목 반복(머리글 반복) → 제거
            if page_changed and norm and any(t == norm for _, t in open_stack):
                prev_page = pg if pg is not None else prev_page
                continue
            while open_stack and open_stack[-1][0] >= lvl:
                open_stack.pop()
            open_stack.append((lvl, norm))
        out.append(e)
        prev_page = pg if pg is not None else prev_page
    return out


def _strip_page_furniture(els: list) -> list:
    """heading 으로 오라벨된 반복 머리말/꼬리말(running header/footer)을 첫 출현만 남기고 제거."""
    pages = {e.get("page_number", 0) for e in els}
    npages = len(pages)
    if npages < 5:
        return els
    freq = {}
    for e in els:
        if (e.get("type") or "").startswith("heading"):
            t = _norm_title(e.get("content", ""))
            if t and len(t) <= 40:
                freq.setdefault(t, set()).add(e.get("page_number", 0))
    # 페이지의 60% 이상(최소 3쪽)에서 heading 으로 반복 → furniture 로 간주.
    furniture = {t for t, ps in freq.items() if len(ps) >= max(3, int(0.6 * npages))}
    if not furniture:
        return els
    logger.info(f"furniture heading 제거(반복 머리말/배너): {sorted(furniture)}")
    kept, out = set(), []
    for e in els:
        if (e.get("type") or "").startswith("heading"):
            t = _norm_title(e.get("content", ""))
            if t in furniture:
                if t in kept:
                    continue              # 반복 출현 제거
                kept.add(t)               # 첫 출현은 보존
        out.append(e)
    return out


def _flat_normalized(doc_dir: str) -> list:
    els = _flat_elements(_load_pages(doc_dir))
    els = _strip_page_furniture(els)          # (0) 반복 머리말/배너 furniture heading 제거
    els = _merge_continued_headings(els)      # (2) 페이지 넘김 반복/연속 heading 병합
    return _normalize_heading_levels(els)     # (1) 번호 정규화 + 구조 클램프


def _split_text(text: str, limit: int) -> list:
    """긴 텍스트를 문장 경계 우선으로 limit 이하 조각들로 분할(임베딩 친화). 초장문은 char 강제 분할."""
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for sent in re.split(r'(?<=[.!?。\n])\s+', text):
        if len(sent) > limit:  # 한 문장이 limit 초과 → char 강제 분할
            if buf:
                parts.append(buf); buf = ""
            for j in range(0, len(sent), limit):
                parts.append(sent[j:j + limit])
            continue
        if buf and len(buf) + len(sent) + 1 > limit:
            parts.append(buf); buf = ""
        buf = f"{buf} {sent}".strip() if buf else sent
    if buf:
        parts.append(buf)
    return parts


def _elem_len(e: dict) -> int:
    """청크 크기 계산용 element 텍스트 길이(content + caption + description)."""
    return sum(len(e.get(k, "") or "") for k in ("content", "caption", "description"))


def _split_oversized(chunks: list, max_chars: int = None) -> list:
    """과도하게 긴 청크를 max_chars 이하로 분할(애매도 폴백 ⑤). element 경계로 묶되,
    단일 element 가 limit 초과면 그 내용까지 문장 단위로 쪼갠다(RAG 임베딩 한계 대응).
    heading 없는 문서의 '거대 단일 청크'도 여기서 윈도우로 쪼개진다."""
    max_chars = max_chars or MAX_CHUNK_CHARS
    overlap = CHUNK_OVERLAP if CHUNK_OVERLAP > 0 else 0
    pack_limit = max(1, max_chars - overlap)   # 오버랩 prepend 후에도 max_chars 이하 보장
    _TABLE_TYPES = {"table"}
    out = []
    for ch in chunks:
        text_len = sum(_elem_len(e) for e in ch.get("elements", []))
        if text_len <= max_chars:
            out.append(ch)
            continue
        # 거대 element 는 내용을 문장 단위 하위 element 로 미리 분해
        pieces = []
        for e in ch["elements"]:
            content = e.get("content", "") or ""
            # 표는 통째로 유지(HTML 셀 중간 절단 방지) — 단일 표가 limit 초과면 그 청크만 초과.
            if len(content) > pack_limit and e.get("type") not in _TABLE_TYPES:
                for frag in _split_text(content, pack_limit):
                    pieces.append({**e, "content": frag})
            else:
                pieces.append(e)
        # 1) 무오버랩 균등 분할(각 하위청크 ≤ pack_limit)
        subs, cur, cur_len, part = [], [], 0, 0
        def flush():
            nonlocal cur, cur_len, part
            if not cur:
                return
            part += 1
            pages = [e.get("page_number", 0) for e in cur]
            sub = dict(ch)
            sub["elements"] = cur
            sub["chunk_id"] = f"{ch['chunk_id']}_p{part}"
            sub["page_range"] = [min(pages), max(pages)] if pages else ch.get("page_range")
            subs.append(sub)
            cur, cur_len = [], 0
        for e in pieces:
            el = _elem_len(e)
            if cur and cur_len + el > pack_limit:
                flush()
            cur.append(e)
            cur_len += el
        flush()
        # 2) 사후 오버랩: 직전 하위청크의 텍스트 꼬리만 prepend(여유분 내).
        if overlap > 0:
            for i in range(1, len(subs)):
                prev_text = " ".join(
                    e.get("content", "") or ""
                    for e in subs[i - 1]["elements"]
                    if e.get("type") not in _TABLE_TYPES and not e.get("_overlap")
                ).strip()
                room = max_chars - sum(_elem_len(e) for e in subs[i]["elements"])
                ov = prev_text[-min(overlap, room):].strip() if room > 0 else ""
                if ov:
                    head = {"type": "text", "content": ov, "_overlap": True,
                            "page_number": subs[i]["elements"][0].get("page_number", 0)}
                    subs[i]["elements"] = [head] + subs[i]["elements"]
        out.extend(subs)
    return out



_BUNJI_RX = re.compile(r'^\[?\s*별\s*[표지]\s*제?\s*\d+\s*(?:호)?\s*(?:서식)?\s*\]?')


def _annex_title(line: str):
    """raw 페이지 첫 줄에서 별표/별지 제목을 추출(없으면 None). VLM 이 누락한 별표 앵커 복구용.
    rhwp 텍스트층은 글자마다 공백이 있어('별 표' / '관 련') 공백 제거 후 매칭한다."""
    line = re.sub(r'\s+', '', line or "")         # 글자별 공백 제거
    if not line:
        return None
    m = _BYLAW_REF_RX.search(line)
    if m and m.end() <= 40:                        # '...심사기준(제5조관련)' 형태
        return line[:m.end()]
    m2 = _BUNJI_RX.match(line)
    if m2:                                          # '[별지제1호서식]' 마커만(서식명은 structured[0])
        return m2.group(0)
    return None


def _recover_annex_title(doc_dir: str, page_num: int, elements: list) -> None:
    """VLM 이 별표/별지 제목줄을 누락한 경우 raw txt 첫 줄에서 복구해 heading 으로 주입한다
    (별표 전체가 직전 별표 밑으로 잘못 중첩되는 것 방지)."""
    if not elements:
        return
    try:
        with open(os.path.join(doc_dir, f"page_{page_num:04d}.txt"), encoding="utf-8") as fp:
            first = next((ln for ln in fp.read().splitlines() if ln.strip()), "")
    except Exception:
        return
    at = _annex_title(first)
    if not at or _heading_rank(at, "heading_1") != 1:
        return
    head0 = (elements[0].get("content", "") or "").strip()
    if head0.startswith(at[:10]) or _heading_rank(head0, elements[0].get("type", "")) == 1:
        return                                    # 이미 앵커가 있으면 주입 안 함
    elements.insert(0, {"type": "heading_1", "content": at,
                        "page_number": page_num, "_annex_recovered": True})


def _load_pages(doc_dir: str) -> list:
    entries = []
    for fname in sorted(f for f in os.listdir(doc_dir) if re.match(r"page_\d+_structured\.json$", f)):
        m = re.match(r"page_(\d+)_structured\.json$", fname)
        if m:
            entries.append((int(m.group(1)), os.path.join(doc_dir, fname)))

    pages, loaded = [], set()
    for page_num, fpath in entries:
        try:
            with open(fpath, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            elements = data.get("elements", [])
            for elem in elements:
                elem.setdefault("page_number", page_num)
            _recover_annex_title(doc_dir, page_num, elements)
            pages.append({"page_number": page_num, "elements": elements})
            loaded.add(page_num)
        except Exception as e:
            logger.warning(f"청킹 로드 실패 {fpath}: {e}")

    # 안전망: structured.json 이 없거나 깨진 페이지(VLM 추출 실패 등)는 원본 텍스트(page_N.txt)
    # 로 폴백해 청크에서 내용이 통째로 누락되는 것을 막는다.
    for fname in sorted(f for f in os.listdir(doc_dir) if re.match(r"page_\d+\.txt$", f)):
        m = re.match(r"page_(\d+)\.txt$", fname)
        if not m or int(m.group(1)) in loaded:
            continue
        pn = int(m.group(1))
        try:
            with open(os.path.join(doc_dir, fname), "r", encoding="utf-8") as fp:
                txt = fp.read().strip()
        except Exception:
            txt = ""
        if txt:
            logger.warning(f"page {pn}: structured.json 없음/오류 → 원본 텍스트로 폴백")
            elements = [{"type": "text", "content": txt, "page_number": pn, "_fallback_text": True}]
            _recover_annex_title(doc_dir, pn, elements)   # 폴백 페이지도 별표/별지 앵커 복구
            pages.append({"page_number": pn, "elements": elements})

    pages.sort(key=lambda p: p["page_number"])
    return pages


def _flat_elements(pages: list) -> list:
    return [
        elem
        for page in pages
        for elem in page["elements"]
        if elem.get("type") != "toc_entry"
    ]


def _load_toc(doc_dir: str) -> list:
    meta_path = os.path.join(doc_dir, "metadata.json")
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f).get("toc", [])
    except Exception:
        return []



def _starts_new_section(els: list) -> bool:
    """페이지 본문 텍스트가 새 상위 섹션으로 시작하면 True(상속 금지).
    편/장/로마는 정밀 패턴으로, 앵커어(별표/부칙/붙임…)는 뒤가 숫자·괄호·EOL일 때만 인정
    ('참고로'·'별표에' 같은 줄글 오인 방지)."""
    for e in els:
        if e.get("type") not in ("text", "paragraph"):
            continue
        lead = (e.get("content") or "").strip().split("\n", 1)[0][:80].lstrip("[ \t")
        if not lead:
            continue
        if _detect_pattern(lead) in ("pyeon", "jang", "roman"):
            return True
        an = _anchor_norm(lead)
        for k in _ANCHOR_KW_N:
            if an.startswith(k):
                rest = an[len(k):]
                # 긴 특정 앵커(신구조문·구조문대비표)는 그대로, 짧은 공통어는 뒤가 숫자·괄호·EOL일 때만.
                if len(k) >= 4 or rest == "" or rest[:1].isdigit() or rest[:1] in "([<":
                    return True
    return False


def chunk_by_page(doc_dir: str) -> list:
    # 1페이지=1청크. heading carry-forward 로 섹션 맥락 부여:
    #  - 자체 헤딩이 있는 페이지 → 그 헤딩 사용(확정).
    #  - 헤딩 없는 페이지 → 마지막 known-good 섹션을 상속하고 _heading_inherited=True 로 표시.
    #    빈 heading_path 는 carry-forward 하지 않아 고아 페이지가 이후를 연쇄로 비우지 않게 한다.
    elements = _flat_normalized(doc_dir)
    heading_stack: list[tuple[int, str]] = []

    def path_from_stack():
        return [title for _, title in heading_stack]

    page_order: list[int] = []
    by_page: dict[int, dict] = {}
    for elem in elements:
        pnum = elem.get("page_number", 0)
        if pnum not in by_page:
            by_page[pnum] = {"elements": [], "own_path": None}
            page_order.append(pnum)
        if elem.get("type") in HEADING_TYPES:                      # 새 헤딩 → 활성 섹션 갱신
            level = HEADING_LEVEL[elem["type"]]
            title = (elem.get("heading_title") or elem.get("content", "")).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            by_page[pnum]["own_path"] = path_from_stack()          # 자체 헤딩 있는 페이지의 섹션
        by_page[pnum]["elements"].append(elem)

    chunks = []
    last_good_path: list = []    # 마지막으로 확정/상속된 비어있지 않은 heading_path
    for pnum in page_order:
        els = [e for e in by_page[pnum]["elements"] if e.get("type") != "toc_entry"]
        if not els:
            continue   # 빈 페이지(예: XLSX 시트 연속 페이지)는 청크 생성 안 함
        own = by_page[pnum]["own_path"]
        inherited = False
        new_sec = _starts_new_section(els)                         # 새 상위 섹션 시작 여부
        if own is not None:                                        # 자체 헤딩 보유 → 확정
            heading_path = own
        elif last_good_path and not new_sec:                       # 헤딩 없는 연속 페이지 → 마지막 known-good 섹션 상속
            heading_path = last_good_path
            inherited = True
        else:
            heading_path = []                                      # 첫 헤딩 이전 · 미식별 새 섹션 → 맥락 없음
            if new_sec:
                last_good_path = []                                # 새 상위 섹션(헤딩 미검출) → 이전 섹션 누수 차단
        chunk = {
            "chunk_id": f"page_{pnum:04d}",
            "chunk_type": "page",
            "page_range": [pnum, pnum],
            "heading_path": heading_path,
            "elements": els,
        }
        if inherited:
            chunk["_heading_inherited"] = True                     # 상속(추정) 표시 — RAG에서 가중조절용
        chunks.append(chunk)
        if heading_path:
            last_good_path = heading_path                          # 고아 빈 path 로는 덮어쓰지 않음(cascade 방지)
    return _split_oversized(chunks)   # XLSX 등 한 페이지에 표 배치가 많은 경우 MAX 초과 분할



def chunk_by_toc(doc_dir: str) -> list:
    elements = _flat_normalized(doc_dir)
    if not elements:
        return []

    chunks = []
    current = None
    heading_stack: list[tuple[int, str]] = []
    counter = 0

    def save(chunk):
        if chunk and chunk["elements"]:
            chunks.append(chunk)

    def path_from_stack():
        return [title for _, title in heading_stack]

    for elem in elements:
        etype = elem.get("type", "")
        pnum = elem.get("page_number", 0)

        if etype in HEADING_TYPES:
            save(current)
            level = HEADING_LEVEL[etype]
            title = (elem.get("heading_title") or elem.get("content", "")).strip()

            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))

            counter += 1
            current = {
                "chunk_id": f"toc_{counter:04d}",
                "chunk_type": "toc",
                "page_range": [pnum, pnum],
                "heading_path": path_from_stack(),
                "elements": [elem],
            }
        else:
            if current is None:
                counter += 1
                current = {
                    "chunk_id": f"toc_{counter:04d}",
                    "chunk_type": "toc",
                    "page_range": [pnum, pnum],
                    "heading_path": [],
                    "elements": [],
                }
            current["elements"].append(elem)
            current["page_range"][1] = pnum

    save(current)
    return _split_oversized(chunks)



def chunk_by_tree(doc_dir: str) -> list:
    elements = _flat_normalized(doc_dir)
    if not elements:
        return []

    nodes = []
    heading_stack: list[tuple[int, str]] = []
    counter = 0

    current = {
        "chunk_id": "tree_0000",
        "chunk_type": "tree",
        "depth": 0,
        "heading_path": [],
        "page_range": [None, None],
        "elements": [],
    }

    def touch_page(node, pnum):
        if node["page_range"][0] is None:
            node["page_range"][0] = pnum
        node["page_range"][1] = pnum

    for elem in elements:
        etype = elem.get("type", "")
        pnum = elem.get("page_number", 0)

        if etype in HEADING_TYPES:
            if current["elements"]:
                if current["page_range"][0] is None:
                    current["page_range"] = [pnum, pnum]
                nodes.append(current)

            level = HEADING_LEVEL[etype]
            title = (elem.get("heading_title") or elem.get("content", "")).strip()

            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))

            counter += 1
            current = {
                "chunk_id": f"tree_{counter:04d}",
                "chunk_type": "tree",
                "depth": level,
                "heading_path": [t for _, t in heading_stack],
                "page_range": [pnum, pnum],
                "elements": [elem],
            }
        else:
            touch_page(current, pnum)
            current["elements"].append(elem)

    if current["elements"]:
        if current["page_range"][0] is None:
            current["page_range"] = [0, 0]
        nodes.append(current)

    return _split_oversized(nodes)



_STRATEGY_MAP = {
    "page": chunk_by_page,
    "toc":  chunk_by_toc,
    "tree": chunk_by_tree,
}


def chunk_document(doc_dir: str, strategies: list = None) -> dict:
    if strategies is None:
        strategies = ["page", "toc", "tree"]

    files = os.listdir(doc_dir)
    has_json = any(re.match(r"page_\d+_structured\.json$", f) for f in files)
    if not has_json:
        # VLM 구조(heading) 없음 → toc/tree 는 만들 수 없다. 렌더 텍스트(page_*.txt)가 있으면
        # page 청킹만 폴백 제공(VLM 미연결·전체 실패 시에도 최소한의 결과).
        if not any(re.match(r"page_\d+\.txt$", f) for f in files):
            logger.info(f"청킹 건너뜀 (구조·텍스트 결과 없음): {doc_dir}")
            return {}
        skipped = [s for s in strategies if s != "page"]
        if skipped:
            logger.warning(f"VLM 구조 없음 → {skipped} 불가(heading 없음), page 청킹만 수행: {doc_dir}")
        strategies = ["page"]

    results = {}
    for strat in strategies:
        fn = _STRATEGY_MAP.get(strat)
        if fn is None:
            logger.warning(f"알 수 없는 청킹 전략: {strat}")
            continue
        try:
            chunks = fn(doc_dir)
            results[strat] = chunks
            out_path = os.path.join(doc_dir, f"split_{strat}.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(chunks, f, ensure_ascii=False, indent=2)
            logger.info(f"텍스트 분할 완료 [{strat}]: {len(chunks)}개 → {out_path}")
        except Exception as e:
            logger.error(f"청킹 오류 [{strat}]: {e}")

    return results
