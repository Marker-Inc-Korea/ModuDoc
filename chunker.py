import os
import json
import re
import logging

logger = logging.getLogger(__name__)

HEADING_TYPES = {"heading_1", "heading_2", "heading_3"}
HEADING_LEVEL = {"heading_1": 1, "heading_2": 2, "heading_3": 3}



def _load_pages(doc_dir: str) -> list:
    entries = []
    for fname in sorted(f for f in os.listdir(doc_dir) if re.match(r"page_\d+_structured\.json$", f)):
        m = re.match(r"page_(\d+)_structured\.json$", fname)
        if m:
            entries.append((int(m.group(1)), os.path.join(doc_dir, fname)))

    pages = []
    for page_num, fpath in entries:
        try:
            with open(fpath, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            elements = data.get("elements", [])
            for elem in elements:
                elem.setdefault("page_number", page_num)
            pages.append({"page_number": page_num, "elements": elements})
        except Exception as e:
            logger.warning(f"청킹 로드 실패 {fpath}: {e}")
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



def chunk_by_page(doc_dir: str) -> list:
    pages = _load_pages(doc_dir)
    return [
        {
            "chunk_id": f"page_{p['page_number']:04d}",
            "chunk_type": "page",
            "page_range": [p["page_number"], p["page_number"]],
            "heading_path": [],
            "elements": [e for e in p["elements"] if e.get("type") != "toc_entry"],
        }
        for p in pages
    ]



def chunk_by_toc(doc_dir: str) -> list:
    elements = _flat_elements(_load_pages(doc_dir))
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
            title = elem.get("content", "").strip()

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
    return chunks



def chunk_by_tree(doc_dir: str) -> list:
    elements = _flat_elements(_load_pages(doc_dir))
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
            title = elem.get("content", "").strip()

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

    return nodes



_STRATEGY_MAP = {
    "page": chunk_by_page,
    "toc":  chunk_by_toc,
    "tree": chunk_by_tree,
}


def chunk_document(doc_dir: str, strategies: list = None) -> dict:
    if strategies is None:
        strategies = ["page", "toc", "tree"]

    has_json = any(
        re.match(r"page_\d+_structured\.json$", f)
        for f in os.listdir(doc_dir)
    )
    if not has_json:
        logger.info(f"청킹 건너뜀 (JSON 결과 없음): {doc_dir}")
        return {}

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
