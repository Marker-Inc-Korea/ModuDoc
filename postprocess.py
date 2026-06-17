import argparse
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path



def get_sheet_for_page(page_num: int, sheet_map: list) -> str | None:
    for s in sheet_map:
        if s["page_start"] <= page_num <= s["page_end"]:
            return s["name"]
    return None


def breadcrumb_str(bc: dict, sheet_name: str | None = None) -> str:
    parts = []
    if sheet_name:
        parts.append(sheet_name)
    parts += [bc[k] for k in ("heading_1", "heading_2", "heading_3") if bc.get(k)]
    return " > ".join(parts)


def has_markdown_header(md: str) -> bool:
    lines = [l.strip() for l in md.strip().splitlines() if l.strip()]
    return len(lines) >= 2 and bool(re.match(r"\|[-| :]+\|", lines[1]))


def get_table_header_lines(md: str) -> list[str]:
    lines = [l.strip() for l in md.strip().splitlines() if l.strip()]
    if len(lines) >= 2 and re.match(r"\|[-| :]+\|", lines[1]):
        return lines[:2]
    return []


def merge_table_markdown(prev_md: str, curr_md: str) -> str:
    header_lines = get_table_header_lines(prev_md)
    curr_lines = [l.strip() for l in curr_md.strip().splitlines() if l.strip()]

    if has_markdown_header(curr_md):
        data_lines = curr_lines[2:]
    else:
        data_lines = curr_lines

    if header_lines:
        return "\n".join(header_lines + data_lines)
    return "\n".join(data_lines)



def process_json(doc_output_dir: str, metadata: dict) -> list[dict]:
    page_files = sorted(
        [f for f in os.listdir(doc_output_dir) if re.match(r"page_\d+_structured\.json$", f)],
        key=lambda x: int(re.search(r"page_(\d+)_structured", x).group(1))
    )

    sheet_map = metadata.get("sheets", [])
    bc = {"heading_1": None, "heading_2": None, "heading_3": None}
    chunks = []
    prev_page_last_table_idx = None
    prev_sheet = None

    for page_file in page_files:
        page_num = int(re.search(r"page_(\d+)_structured", page_file).group(1))
        sheet_name = get_sheet_for_page(page_num, sheet_map)
        if sheet_name != prev_sheet:
            bc = {"heading_1": None, "heading_2": None, "heading_3": None}
            prev_sheet = sheet_name
        path = os.path.join(doc_output_dir, page_file)

        try:
            with open(path, "r", encoding="utf-8") as f:
                page_data = json.loads(f.read())
        except (json.JSONDecodeError, OSError):
            prev_page_last_table_idx = None
            continue

        elements = page_data.get("elements", [])
        this_page_last_table_idx = None

        for i, elem in enumerate(elements):
            etype = elem.get("type", "")
            content = elem.get("content", "").strip()
            caption = elem.get("caption", "")

            if etype in ("heading_1", "heading_2", "heading_3"):
                if etype == "heading_1":
                    bc["heading_1"] = content
                    bc["heading_2"] = None
                    bc["heading_3"] = None
                elif etype == "heading_2":
                    bc["heading_2"] = content
                    bc["heading_3"] = None
                else:
                    bc["heading_3"] = content
                prev_page_last_table_idx = None
                this_page_last_table_idx = None
                chunks.append({
                    **metadata,
                    "page": page_num,
                    "type": etype,
                    "content": content,
                    "breadcrumb": breadcrumb_str(bc, sheet_name),
                })

            elif etype == "table":
                is_first_elem = (i == 0)
                if is_first_elem and prev_page_last_table_idx is not None:
                    prev_chunk = chunks[prev_page_last_table_idx]
                    merged = merge_table_markdown(prev_chunk["content"], content)
                    prev_chunk["content"] = merged
                    prev_chunk["page_end"] = page_num
                    if caption and not prev_chunk.get("caption"):
                        prev_chunk["caption"] = caption
                    this_page_last_table_idx = prev_page_last_table_idx
                else:
                    chunk = {
                        **metadata,
                        "page": page_num,
                        "type": "table",
                        "content": content,
                        "breadcrumb": breadcrumb_str(bc, sheet_name),
                    }
                    if caption:
                        chunk["caption"] = caption
                    chunks.append(chunk)
                    this_page_last_table_idx = len(chunks) - 1

            else:
                prev_page_last_table_idx = None
                this_page_last_table_idx = None
                chunks.append({
                    **metadata,
                    "page": page_num,
                    "type": etype,
                    "content": content,
                    "breadcrumb": breadcrumb_str(bc, sheet_name),
                })

        prev_page_last_table_idx = this_page_last_table_idx

    return chunks



def process_xml(doc_output_dir: str, metadata: dict) -> list[dict]:
    page_files = sorted(
        [f for f in os.listdir(doc_output_dir) if re.match(r"page_\d+_structured\.xml$", f)],
        key=lambda x: int(re.search(r"page_(\d+)_structured", x).group(1))
    )

    sheet_map = metadata.get("sheets", [])
    bc = {"heading_1": None, "heading_2": None, "heading_3": None}
    chunks = []
    prev_page_last_table_idx = None
    prev_sheet = None

    for page_file in page_files:
        page_num = int(re.search(r"page_(\d+)_structured", page_file).group(1))
        sheet_name = get_sheet_for_page(page_num, sheet_map)
        if sheet_name != prev_sheet:
            bc = {"heading_1": None, "heading_2": None, "heading_3": None}
            prev_sheet = sheet_name
        path = os.path.join(doc_output_dir, page_file)

        try:
            tree = ET.parse(path)
            root = tree.getroot()
        except ET.ParseError:
            prev_page_last_table_idx = None
            continue

        elements_node = root.find("elements")
        if elements_node is None:
            prev_page_last_table_idx = None
            continue

        elems = list(elements_node)
        this_page_last_table_idx = None

        for i, elem in enumerate(elems):
            etype = elem.get("type", "")
            content = (elem.text or "").strip()
            caption = elem.get("caption", "")

            if etype in ("heading_1", "heading_2", "heading_3"):
                if etype == "heading_1":
                    bc["heading_1"] = content
                    bc["heading_2"] = None
                    bc["heading_3"] = None
                elif etype == "heading_2":
                    bc["heading_2"] = content
                    bc["heading_3"] = None
                else:
                    bc["heading_3"] = content
                prev_page_last_table_idx = None
                this_page_last_table_idx = None
                chunks.append({
                    **metadata,
                    "page": page_num,
                    "type": etype,
                    "content": content,
                    "breadcrumb": breadcrumb_str(bc, sheet_name),
                })

            elif etype == "table":
                is_first_elem = (i == 0)
                if is_first_elem and prev_page_last_table_idx is not None:
                    prev_chunk = chunks[prev_page_last_table_idx]
                    merged = merge_table_markdown(prev_chunk["content"], content)
                    prev_chunk["content"] = merged
                    prev_chunk["page_end"] = page_num
                    if caption and not prev_chunk.get("caption"):
                        prev_chunk["caption"] = caption
                    this_page_last_table_idx = prev_page_last_table_idx
                else:
                    chunk = {
                        **metadata,
                        "page": page_num,
                        "type": "table",
                        "content": content,
                        "breadcrumb": breadcrumb_str(bc, sheet_name),
                    }
                    if caption:
                        chunk["caption"] = caption
                    chunks.append(chunk)
                    this_page_last_table_idx = len(chunks) - 1

            else:
                prev_page_last_table_idx = None
                this_page_last_table_idx = None
                chunks.append({
                    **metadata,
                    "page": page_num,
                    "type": etype,
                    "content": content,
                    "breadcrumb": breadcrumb_str(bc, sheet_name),
                })

        prev_page_last_table_idx = this_page_last_table_idx

    return chunks



def run(doc_output_dir: str, fmt: str = "json"):
    meta_path = os.path.join(doc_output_dir, "metadata.json")
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
    else:
        metadata = {}

    if fmt == "xml":
        chunks = process_xml(doc_output_dir, metadata)
    else:
        chunks = process_json(doc_output_dir, metadata)

    out_path = os.path.join(doc_output_dir, "chunks.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)

    print(f"chunks.json 저장 완료: {len(chunks)}개 청크 → {out_path}")
    return chunks


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAG 후처리: breadcrumb + 테이블 병합")
    parser.add_argument("doc_output_dir", help="page_N_structured.json/xml 파일이 있는 디렉터리")
    parser.add_argument("--format", choices=["json", "xml"], default="json")
    args = parser.parse_args()
    run(args.doc_output_dir, args.format)
