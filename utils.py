import os
import re
import sys
import uuid
import json
import shutil
import base64
import logging
import platform
import subprocess
import tempfile
import threading
import time
import zipfile
import unicodedata
import xml.etree.ElementTree as ET

_VLM_SEMAPHORE = threading.Semaphore(5)

import fitz
import openpyxl
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from openpyxl.worksheet.page import PageMargins
from bs4 import BeautifulSoup, NavigableString

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class HWPTextExtractor:
    """HWP/HWPX 문서에서 텍스트 및 구조를 추출하는 파서 클래스."""

    @staticmethod
    def text_preprocessing(text, threshold=0.5):
        """텍스트를 정규화하고 노이즈(URL, 특수문자 과다 등)를 제거한다."""
        if not text: return ""
        special_chars = re.sub(r'[가-힣a-zA-Z0-9\s]', '', text)
        if len(text) > 0 and (len(special_chars) / len(text)) >= threshold: return ""
        
        url_pattern = r"https?://\S+|www\.\S+"
        email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
        text = re.sub(url_pattern, " ", text)
        text = re.sub(email_pattern, " ", text)
        
        text = unicodedata.normalize("NFC", text)
        text = re.sub(r'[\uE000-\uF8FF]', "", text)
        text = re.sub("\u00A0", " ", text)
        text = re.sub(r'\n+', '\n', text)
        text = re.sub(r' +', ' ', text)
        return text.strip()

    @classmethod
    def hwp_process_children(cls, tag):
        """HTML 태그의 자식 노드를 재귀적으로 순회하여 콘텐츠 문자열을 반환한다."""
        content = ""
        for child in tag.children:
            if isinstance(child, NavigableString):
                content += str(child)
            elif child.name == 'table':
                content += cls.hwp_parse_table(child)
            elif child.name == 'p':
                inner_html = cls.hwp_process_children(child)
                content += f"<p>{inner_html}</p>"
            elif child.name == 'img':
                src = child.get('src', '')
                content += f'<img src="{src}" alt="이미지"/>'
            elif hasattr(child, 'prettify'):
                content += child.prettify(formatter="html5")
        return content.strip()

    @classmethod
    def hwp_parse_table(cls, table_tag):
        """BeautifulSoup table 태그를 HTML 문자열로 직렬화한다."""
        table_html = "<table>"
        for tr in table_tag.find_all('tr', recursive=False):
            table_html += "<tr>"
            for td in tr.find_all('td', recursive=False):
                col = td.get('colspan', 1)
                row = td.get('rowspan', 1)
                table_html += f'<td colspan="{col}" rowspan="{row}">'
                table_html += cls.hwp_process_children(td)
                table_html += "</td>"
            table_html += "</tr>"
        table_html += "</table>"
        for child in table_tag.children:
            if child.name == 'caption':
                table_html += f"<p>{cls.hwp_process_children(child)}</p>"
        return table_html

    @classmethod
    def parser_from_hwp_html(cls, file_path):
        """hwp5html로 HWP를 XHTML 변환 후 페이지 단위 HTML 문자열 목록을 반환한다."""
        temp_dir = tempfile.mkdtemp()
        pages = []
        try:
            subprocess.run(['hwp5html', '--output', temp_dir, file_path], check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            xhtml_file_path = os.path.join(temp_dir, "index.xhtml")
            with open(xhtml_file_path, "r", encoding="utf-8") as f:
                soup = BeautifulSoup(f.read(), 'html.parser').body

            for span_tag in soup.find_all('span'): span_tag.unwrap()
            
            all_pages = soup.find_all('div', class_='Page')
            if not all_pages: all_pages = [soup.body]
            
            for content_area in all_pages:
                current_page_text = []
                for element in content_area.children:
                    if element.name == 'table':
                        current_page_text.append(cls.hwp_parse_table(element).strip())
                    elif element.name == 'p':
                        if element.find(attrs={'class': 'GShapeObjectControl'}): continue 
                        table_in_p = element.find('table')
                        if table_in_p:
                            current_page_text.append(cls.hwp_parse_table(table_in_p).strip())
                        else:
                            processed_html = cls.hwp_process_children(element) + "<br>"
                            current_page_text.append(processed_html)
                    elif element.name == 'img':
                        src = element.get('src', "")
                        current_page_text.append(f"<img src='{src}' alt='이미지'/>")
                
                page_str = "<br>".join(current_page_text).replace('`', "'")
                page_str = re.sub(r"(<br> ){2,}", "<br>", page_str).strip()
                pages.append(re.sub(r"(<br>){2,}", "<br>", page_str).strip())

        except Exception as error:
            logger.warning(f"HTML fallback 파싱 에러: {error}")
        finally:
            shutil.rmtree(temp_dir)
        return pages

    @classmethod
    def get_text_from_hwpx(cls, element):
        """HWPX XML p 요소에서 텍스트와 인라인 오브젝트를 추출한다."""
        text = []
        sentence_tags = element.findall('./{*}run')
        for sentence_tag in sentence_tags:
            for child in sentence_tag:
                child_tag = child.tag.split('}')[-1]
                if child_tag == "t": text.append(child.text)
                elif child_tag == "fwSpace": text.append(" ")
                elif child_tag in ["pic", "rect", "poly", "ellipse"]:
                    obj_id = child.get("id", "")
                    if child.find("./{*}drawText") is not None:
                        obj_text = ""
                        for t_tag in child.findall("./{*}drawText/{*}subList/{*}p"):
                            obj_text += cls.get_text_from_hwpx(t_tag)
                        text.append(f"<{child_tag} id='{obj_id}'> {obj_text} </{child_tag}>")
                    else:
                        text.append(f"<{child_tag} id='{obj_id}' alt='이미지'/>")
        text = [t for t in text if t is not None]
        return "".join(text).replace('`', "'")

    @classmethod
    def get_table_from_hwpx(cls, element):
        """HWPX XML tbl 요소를 HTML 테이블 문자열로 변환한다."""
        table = "<table>"
        rows = element.findall('./{*}tr')
        for row in rows:
            table += "<tr>"
            for cell in row.findall('./{*}tc'):
                span = cell.find('./{*}cellSpan')
                colspan = span.get('colSpan', '1') if span is not None else '1'
                rowspan = span.get('rowSpan', '1') if span is not None else '1'
                table_in_cell = cell.findall('./{*}subList/{*}p/{*}run/{*}tbl')
                table += f"<td colspan='{colspan}' rowspan='{rowspan}'>"
                if table_in_cell:
                    for inner_table in table_in_cell: table += cls.get_table_from_hwpx(inner_table)
                else:
                    for cell_text_tag in cell.findall('./{*}subList/{*}p'):
                        table += cls.get_text_from_hwpx(cell_text_tag)
                table += "</td>"
            table += "</tr>"
        return table + "</table>"

    @classmethod
    def parser_from_hwpx_xml(cls, file_path):
        """HWPX ZIP 내 section XML을 파싱하여 페이지 단위 HTML 문자열 목록을 반환한다."""
        pages = []
        try:
            all_p_elements = []
            with zipfile.ZipFile(file_path, 'r') as zf:
                section_files = sorted(
                    [n for n in zf.namelist() if re.match(r'Contents/section\d+\.xml', n)],
                    key=lambda x: int(re.search(r'\d+', x).group())
                )
                for sec_file in section_files:
                    xml_content = zf.read(sec_file).decode('utf-8')
                    root = ET.fromstring(xml_content)
                    all_p_elements.extend(root.findall('./{*}p'))

            current_page = []
            for p_element in all_p_elements:
                has_break = any(k.endswith('pageBreak') and v == "1"
                                for k, v in p_element.attrib.items())
                if has_break and current_page:
                    pages.append("<br>".join(current_page))
                    current_page = []

                table = p_element.find('./{*}run/{*}tbl')
                if table is not None:
                    current_page.append(cls.get_table_from_hwpx(table))
                else:
                    text = cls.get_text_from_hwpx(p_element)
                    if text: current_page.append(text)

            if current_page: pages.append("<br>".join(current_page))
        except Exception as e:
            logger.error(f"HWPX 파싱 에러: {e}")
        return [re.sub(r"(<br>){2,}", "<br>", p).strip() for p in pages]

    @classmethod
    def _process_hwp_child(cls, child, text, include_table=True):
        """HWP XML 자식 요소에서 텍스트/테이블/도형 추출 헬퍼."""
        if child.tag == "Text":
            text.append(child.text)
        elif child.tag == "GShapeObjectControl":
            shp_comp = child.find("./ShapeComponent")
            if shp_comp is not None:
                text_tag = shp_comp.find("TextboxParagraphList")
                gso_type = shp_comp.get("chid")
                if gso_type == "$pic":
                    picture_id = child.get("instance-id", "")
                    if text_tag is not None:
                        pic_text = "".join(cls.get_text_from_hwp(p) for p in text_tag.findall("./Paragraph"))
                        text.append(f"<img id='{picture_id}'> {pic_text} </img>")
                    else:
                        text.append(f"<img id='{picture_id}' alt='이미지'/>")
                elif gso_type == "$rec":
                    rect_id = child.get("instance-id", "")
                    if text_tag is not None:
                        rect_text = "".join(cls.get_text_from_hwp(p) for p in text_tag.findall("./Paragraph"))
                        text.append(f"<rect id='{rect_id}'> {rect_text} </rect>")
        elif child.tag == "TableControl" and include_table:
            table_html = cls.get_table_from_hwp(child)
            if table_html: text.append(table_html)

    @classmethod
    def get_text_from_hwp(cls, element):
        """HWP XML Paragraph 요소에서 텍스트와 인라인 오브젝트를 추출한다."""
        text = []
        for sentence_tag in element.findall('./LineSeg'):
            fieldclickheres = sentence_tag.findall('./FieldClickHere')
            if fieldclickheres:
                for fch in fieldclickheres:
                    inner_fchs = fch.findall('./FieldClickHere')
                    if inner_fchs:
                        for inner_fch in inner_fchs:
                            for child in inner_fch:
                                cls._process_hwp_child(child, text)
                    else:
                        for child in fch:
                            cls._process_hwp_child(child, text)
            else:
                for child in sentence_tag:
                    cls._process_hwp_child(child, text)
        text = [t for t in text if t is not None]
        return "".join(text)

    @classmethod
    def _get_text_from_hwp_for_table(cls, element):
        """테이블 셀 내 Paragraph 텍스트 추출 (TableControl 재귀 없이)."""
        text = []
        for sentence_tag in element.findall('./LineSeg'):
            fieldclickheres = sentence_tag.findall('./FieldClickHere')
            if fieldclickheres:
                for fch in fieldclickheres:
                    inner_fchs = fch.findall('./FieldClickHere')
                    if inner_fchs:
                        for inner_fch in inner_fchs:
                            for child in inner_fch:
                                cls._process_hwp_child(child, text, include_table=False)
                    else:
                        for child in fch:
                            cls._process_hwp_child(child, text, include_table=False)
            else:
                for child in sentence_tag:
                    cls._process_hwp_child(child, text, include_table=False)
        text = [t for t in text if t is not None]
        return "".join(text)

    @classmethod
    def get_table_from_hwp(cls, element):
        """HWP XML TableControl 요소를 HTML 테이블 문자열로 변환한다."""
        table_body = element.find('./TableBody')
        if table_body is None: return ""
        table = "<table>"
        for row in table_body.findall('./TableRow'):
            table += "<tr>"
            for cell in row.findall('./TableCell'):
                colspan, rowspan = cell.get('colspan', '1'), cell.get('rowspan', '1')
                table += f"<td colspan='{colspan}' rowspan='{rowspan}'>"
                table_in_cell = cell.findall('./Paragraph/LineSeg/TableControl')
                table_in_cell_ex = cell.findall('./ColumnSet/Paragraph/LineSeg/TableControl')
                if table_in_cell:
                    for it in table_in_cell: table += cls.get_table_from_hwp(it)
                elif table_in_cell_ex:
                    for it in table_in_cell_ex: table += cls.get_table_from_hwp(it)
                else:
                    columnsets = cell.findall('./ColumnSet')
                    if columnsets:
                        for cs in columnsets:
                            for p in cs.findall('./Paragraph'):
                                table += cls._get_text_from_hwp_for_table(p)
                    else:
                        for p in cell.findall('./Paragraph'):
                            table += cls._get_text_from_hwp_for_table(p)
                table += "</td>"
            table += "</tr>"
        return table + "</table>"

    @classmethod
    def parser_from_hwp_xml(cls, file_path):
        """hwp5proc xml 출력을 파싱하여 페이지 단위 HTML 문자열 목록을 반환한다."""
        pre, _ = os.path.splitext(file_path)
        temp_xml = pre + f"_{uuid.uuid4().hex[:6]}_temp.xml"
        pages = []
        try:
            with open(temp_xml, 'w', encoding='utf-8') as f:
                subprocess.run(['hwp5proc', 'xml', file_path], stdout=f, stderr=subprocess.DEVNULL, check=True)
            
            root = ET.parse(temp_xml).getroot()
            current_page = []
            
            for p_element in root.findall('.//SectionDef/ColumnSet/Paragraph'):
                has_break = any(k.endswith('new-page') and v == "1"
                                for k, v in p_element.attrib.items())
                if has_break and current_page:
                    pages.append("<br>".join(current_page))
                    current_page = []
                text = cls.get_text_from_hwp(p_element)
                if text: current_page.append(text)
            
            if current_page: pages.append("<br>".join(current_page))
        except Exception as e:
            raise e
        finally:
            if os.path.exists(temp_xml): os.remove(temp_xml)
            
        return [re.sub(r"(<br>){2,}", "<br>", p).strip() for p in pages]

    @classmethod
    def extract_pages(cls, file_path):
        """ 메인 진입점: HWPX/HWP를 판별하여 페이지 단위 텍스트 배열을 반환합니다. """
        _, ext = os.path.splitext(file_path)
        ext = ext.lower()
        pages = []
        
        if ext == ".hwpx":
            pages = cls.parser_from_hwpx_xml(file_path)
        elif ext == ".hwp":
            pages = cls.parser_from_hwp_html(file_path)
            if not pages:
                try:
                    pages = cls.parser_from_hwp_xml(file_path)
                except Exception:
                    pages = []

        return [cls.text_preprocessing(p) for p in pages if cls.text_preprocessing(p)]


def _sanitize_json_strings(text: str) -> str:
    """JSON 문자열 값 내부의 control character 및 invalid escape를 교체."""
    _CTRL_ESCAPES = {'\n': '\\n', '\r': '\\r', '\t': '\\t'}
    _VALID_ESCAPES = set('"\\\/bfnrtu')
    result = []
    in_string = False
    i = 0
    while i < len(text):
        c = text[i]
        if in_string:
            if c == '\\':
                i += 1
                if i < len(text):
                    nc = text[i]
                    if nc in _VALID_ESCAPES:
                        result.append('\\')
                        result.append(nc)
                    else:
                        result.append('\\\\')
                        result.append(nc)
                else:
                    result.append('\\\\')
            elif c == '"':
                result.append(c)
                in_string = False
            elif ord(c) < 0x20 or c == '\x7f':
                result.append(_CTRL_ESCAPES.get(c, ''))
            else:
                result.append(c)
        else:
            if c == '"':
                in_string = True
            result.append(c)
        i += 1
    return ''.join(result)


class VLMProcessor:
    """Together AI VLM을 통해 문서 페이지를 구조화된 JSON/XML로 변환하는 처리기."""

    @classmethod
    def encode_image(cls, image_path, max_width=1024):
        """이미지를 base64로 인코딩. VLM 전송용으로 max_width 이하로 리사이즈."""
        try:
            from PIL import Image
            import io
            with Image.open(image_path) as img:
                if img.width > max_width:
                    ratio = max_width / img.width
                    new_size = (max_width, int(img.height * ratio))
                    img = img.resize(new_size, Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                return base64.b64encode(buf.getvalue()).decode('utf-8')
        except ImportError:
            with open(image_path, "rb") as f:
                return base64.b64encode(f.read()).decode('utf-8')

    @classmethod
    def extract_structure(cls, txt_path, img_path=None, api_key=None, output_format="json", model_name="Qwen/Qwen3-VL-8B-Instruct"):
        """VLM에 텍스트와 이미지를 전송하여 구조화된 JSON 또는 XML 문자열을 반환한다."""
        if not api_key or "여기에_" in api_key: return None

        try:
            from together import Together
            client = Together(api_key=api_key, timeout=300, max_retries=0)
        except Exception as e:
            logger.error(f"Together 오류: {e}")
            return None

        with open(txt_path, "r", encoding="utf-8") as f:
            extracted_text = f.read()

        if output_format.lower() == "xml":
            schema_instruction = """[FORMAT INSTRUCTION: XML ONLY]
CRITICAL: You MUST output ONLY valid XML. Do NOT output JSON format under any circumstances.
You MUST extract the contents of the page strictly in the sequential order they appear visually.
Use the following exact XML template:

<document>
    <page_number>int</page_number>
    <elements>
        <element type="heading_1">Top level heading (e.g., Ⅰ., 1.)</element>
        <element type="heading_2">Second level heading (e.g., 가., 1.1)</element>
        <element type="heading_3">Third level heading (e.g., 1), 가)</element>
        <element type="toc_entry">TOC item — content format: "title::page_number"</element>
        <element type="text">General paragraph text</element>
        <element type="table" caption="Table title or caption if available">Markdown table content</element>
        <element type="figure" caption="Figure title or caption if available" description="Detailed visual description: for charts/graphs describe what it shows, trends, key observations; for photographs describe what is depicted">For charts/graphs: extracted visible text data here (axis labels, tick values, etc.). Empty for photographs.</element>
        <element type="footnote">Footnote or reference text at the bottom</element>
    </elements>
</document>

Rules for XML:
1. Reading order:
   - Single-column: read top to bottom.
   - Multi-column (2 or more columns): read the LEFT column fully (top to bottom), then the RIGHT column (top to bottom).
   - Use the layout image to determine the number of columns.
2. Repeat <element> tags as needed — multiple elements of the same type are allowed.
3. If an element does not exist, simply do not include that type.
4. [TABLE — CRITICAL] Any grid of data with rows and columns MUST be output as HTML table. Rules:
   a. Use ONLY <table>, <tr>, <td> tags. NEVER use <th>. You MAY use colspan/rowspan (e.g. <td colspan="2">Header</td>).
   b. Always use the layout IMAGE to detect tables visually — raw text extraction often loses column alignment and merges cells.
   c. Include ALL rows: header rows, subheader rows, data rows, and any title row that spans all columns.
   d. Each row must have the same number of cells (accounting for colspan/rowspan). Empty cells must be included as empty <td></td>.
   e. If a table has a multi-row header (e.g. a "Training Datasets" row spanning 6 columns, then "Instruction / Alignment" row spanning 3 each), reproduce all those rows with correct colspan.
   f. NEVER output table data as flat text or bullet points — always use <table> structure.
5. Type rules:
   - heading_1/2/3: For actual section titles and prominent standalone headings. Use for: section titles in body text (e.g. "Introduction", "1.1 Methods"); on slide/presentation pages, EACH visually prominent section label and numbered step title MUST be a SEPARATE heading element — do NOT merge section labels with their body text (e.g. "Our Purpose" → heading_2, "Making AI Beneficial" → text; "01 - Find Resources" → heading_2, description → text); section labels that introduce a data block on a report/survey page (e.g. "Education Level", "Profession"). Do NOT use for: TOC entries; run-in paragraph starters — if a "heading" would contain a period in the MIDDLE followed by more text (e.g. "Business characteristics. Business size was determined by...", "Ablation on the SFT base models. We compare...", "Filtered task names. We present task names..."), it is a paragraph, NOT a heading — output as text with the opening label in **bold**; bold short labels ending with a period followed by body text (e.g. "Base model.", "Depthwise scaling." — use text element); lettered sub-items (a., b., c., d.) that introduce paragraphs.
   - toc_entry: ONLY for items on a Table of Contents page. Format content as "title::page_number".
   - figure: ONLY for actual embedded images, photographs, charts, or diagrams — NOT for text formatted with symbols like ▴, ●, ○, etc. Put the caption in the caption attribute. Always fill the description attribute with a detailed visual description: for charts/graphs describe what the chart shows, the overall trend, and key observations; for photographs describe what is depicted. For charts/graphs/plots: scan the ENTIRE chart image from top-left to bottom-right and output EVERY visible number, label, and text exactly as it appears into the element body — including Y-axis values (top to bottom), X-axis labels, bar/line data labels, legend entries, percentage labels, and any annotations. List each item on its own line in visual reading order (top-to-bottom, left-to-right). For photographs or decorative images with no data, leave the element body empty.
   - footnote: ONLY for superscript-style reference notes at the very bottom margin (e.g. *, ①, ※ markers). Do NOT use for regular body paragraphs.
   - table caption: Put the table title in the caption attribute, NOT as a separate heading element.
6. CRITICAL — NO DUPLICATION: Each piece of content must appear exactly ONCE. Never output the same text in multiple elements."""
        else:
            schema_instruction = """[FORMAT INSTRUCTION: JSON ONLY]
CRITICAL: Output ONLY valid JSON.
You MUST extract the contents of the page strictly in the sequential order they appear visually.
Use the following JSON schema:

{
    "page_number": int,
    "elements": [
        {
            "type": "heading_1 | heading_2 | heading_3 | toc_entry | text | table | figure | footnote",
            "content": "Text content or HTML table. For toc_entry: 'title::page_number'. For figure (chart/graph): extracted visible text data (axis labels, tick values, legend, percentages). Empty for photographs.",
            "caption": "Optional: table title or figure caption if present",
            "description": "For figure only: detailed visual description — for charts/graphs describe what it shows, trends, and key observations; for photographs describe what is depicted. Leave empty for non-figure elements."
        }
    ]
}

Rules for JSON:
1. Reading order:
   - Single-column: read top to bottom.
   - Multi-column (2 or more columns): read the LEFT column fully (top to bottom), then the RIGHT column (top to bottom).
   - Use the layout image to determine the number of columns.
2. "type" MUST be one of the specified enum values:
   - heading_1/2/3: For actual section titles and prominent standalone headings. Use for: section titles in body text (e.g. "Introduction", "1.1 Methods"); on slide/presentation pages, EACH visually prominent section label and numbered step title MUST be a SEPARATE heading element — do NOT merge section labels with their body text (e.g. on a slide with "Our Purpose\nMaking AI Beneficial", "Our Purpose" is heading_2 and "Making AI Beneficial" is text; on a slide with "01 - Find Resources\nStart by searching...", "01 - Find Resources" is heading_2 and "Start by searching..." is text); section labels that introduce a data block on a report/survey page (e.g. "Education Level", "Profession"). Do NOT use for: TOC entries, bold inline labels within a paragraph sentence (e.g. "Base model. We trained..." where text continues on the same line).
   - toc_entry: ONLY for items listed on a Table of Contents page. Set content to "title::page_number".
   - text: General body paragraphs, including lists, bullet points, and any text content that is NOT a heading. Use **bold** for bold text within paragraphs.
   - table: [CRITICAL] Any grid of data MUST be output as HTML in "content". Use ONLY <table>, <tr>, <td> — NEVER <th>. You MAY use colspan/rowspan. Always use the layout IMAGE to detect tables visually (raw text loses column structure). Include ALL rows: title rows, multi-row headers (with correct colspan), data rows, totals rows. Empty cells must be <td></td>. NEVER output table data as flat text. Put table title in "caption".
   - figure: ONLY for actual embedded images, photographs, charts, or diagrams that are visual/graphical content — NOT for text formatted with symbols like ▴, ●, ○, ☞, etc. Put caption in "caption". For charts/graphs/plots: scan the ENTIRE chart from top-left to bottom-right and output EVERY visible number, label, and text into "content" — including Y-axis values (top to bottom), X-axis labels, bar/line data labels, legend entries, percentage labels, and annotations. List each item on its own line in visual reading order. For photographs or decorative images with no data, leave "content" empty. Always fill "description" with a detailed visual description: for charts/graphs describe what the chart shows, the overall trend, and key observations (e.g. "Bar chart showing monthly sales from Jan to Dec 2023. Sales peak in December at 120M, with a steady upward trend in Q4."); for photographs describe what is depicted in the image.
   - footnote: ONLY for superscript-style reference notes (e.g., *, ①, ※ markers at the very bottom margin of the page). Do NOT use for regular body text that happens to appear at the bottom.
3. CRITICAL — NO DUPLICATION: Each piece of text content must appear exactly ONCE in the elements array. Never output the same content in multiple elements.
4. If the page is empty, return an empty "elements" array."""

        system_prompt = f"""You are an expert document parsing AI.
Your task is to convert the provided document text (and layout image if available) into a structured format.

[CRITICAL OUTPUT RULES]
- Output ONLY the raw {output_format.upper()} — no explanation, no commentary, no markdown code fences.
- Your entire response must be valid, parseable {output_format.upper()}. If it cannot be parsed, it is wrong.
- Do not truncate or abbreviate content. Every element must be complete.
- Never use unescaped special characters inside JSON strings (e.g. use \\n for newlines, escape double quotes).
- For tables: every Markdown table row must have the same number of columns as the header row.


{schema_instruction}"""

        user_prompt = f"""
Here is the raw text extracted from the document:
<raw_text>
{extracted_text}
</raw_text>

Based on the raw text and the visual layout in the image (if provided), structure the information strictly in {output_format.upper()} format.
"""
        content_payload = [{"type": "text", "text": user_prompt}]

        if img_path and os.path.exists(img_path):
            base64_image = cls.encode_image(img_path)
            content_payload.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{base64_image}"}
            })

        def _strip_fences(text):
            t = text.strip()
            if t.startswith("```json"): t = t[7:]
            elif t.startswith("```xml"): t = t[6:]
            elif t.startswith("```"): t = t[3:]
            if t.endswith("```"): t = t[:-3]
            return t.strip()

        for attempt in range(3):
            try:
                with _VLM_SEMAPHORE:
                    response = client.chat.completions.create(
                        model=model_name,
                        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": content_payload}],
                        temperature=0.1,
                        max_tokens=16384,
                        timeout=300
                    )
                result = _strip_fences(response.choices[0].message.content)

                input_len = len(extracted_text)
                if input_len > 0 and len(result) < input_len * 0.9:
                    logger.warning(f"출력 길이 부족 (시도 {attempt+1}/3): 입력={input_len}, 출력={len(result)} — truncation 의심, 재시도")
                    if attempt < 2:
                        time.sleep(5 * (attempt + 1))
                        continue

                if output_format.lower() == "json":
                    try:
                        json.loads(result)
                    except json.JSONDecodeError as je:
                        sanitized = _sanitize_json_strings(result)
                        try:
                            json.loads(sanitized)
                            result = sanitized
                        except json.JSONDecodeError:
                            logger.warning(f"JSON 파싱 오류 (시도 {attempt+1}/3): {je} — 재시도")
                            if attempt < 2:
                                time.sleep(5 * (attempt + 1))
                            else:
                                logger.error("JSON 유효성 검증 3회 실패, 건너뜀")
                                return None
                            continue

                return result
            except Exception as e:
                logger.warning(f"VLM API 에러 (시도 {attempt+1}/3): {e}")
                if attempt < 2:
                    time.sleep(5 * (attempt + 1))
                else:
                    logger.error("VLM API 3회 실패, 건너뜀")
                    return None

    @classmethod
    def extract_metadata(cls, txt_paths, img_paths, api_key, model_name):
        """문서 1~2페이지에서 글로벌 메타데이터 추출.
        Returns dict: {doc_title, date, organization, author, keywords}
        """
        if not api_key or "여기에_" in api_key:
            return {}
        try:
            from together import Together
            client = Together(api_key=api_key, timeout=60, max_retries=0)
        except Exception as e:
            logger.error(f"Together 오류: {e}")
            return {}

        combined_text = ""
        for p in txt_paths[:2]:
            if p and os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    combined_text += f.read() + "\n"

        system_prompt = """You are a document metadata extraction AI.
Extract global metadata from the provided document cover/table-of-contents pages.
Output ONLY valid JSON with this exact schema — no markdown, no extra text:
{
    "doc_title": "Full document title",
    "date": "Publication or effective date (YYYY-MM-DD or as written)",
    "organization": "Issuing organization or department",
    "author": "Author name(s) or null if not found",
    "keywords": ["keyword1", "keyword2"]
}
If a field cannot be found, use null."""

        content_payload = [{"type": "text", "text": f"<raw_text>\n{combined_text}\n</raw_text>\n\nExtract metadata as JSON."}]
        for ip in img_paths[:2]:
            if ip and os.path.exists(ip):
                content_payload.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{cls.encode_image(ip)}"}
                })

        try:
            with _VLM_SEMAPHORE:
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": content_payload}],
                    temperature=0.0,
                    max_tokens=512
                )
            raw = response.choices[0].message.content.strip()
            if raw.startswith("```"): raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"): raw = raw.rsplit("```", 1)[0]
            return json.loads(raw.strip())
        except Exception as e:
            logger.error(f"메타데이터 추출 실패: {e}")
            return {}


class DocumentProcessor:
    """PDF/Office/HWP 문서를 파싱하고 VLM으로 구조화하는 통합 파이프라인 처리기."""

    @staticmethod
    def get_libreoffice_cmd():
        """플랫폼에 맞는 LibreOffice 실행 파일 경로를 반환한다."""
        if platform.system() == "Linux": return "libreoffice"
        candidates = [r"C:\Program Files\LibreOffice\program\soffice.exe", r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"]
        if shutil.which("soffice"): return "soffice"
        for p in candidates:
            if os.path.exists(p): return p
        return None

    @staticmethod
    def _optimize_excel_layout(input_path, output_path):
        """엑셀 PDF 변환 전 레이아웃 최적화: A2 가로, 1페이지 너비 맞춤, 여백 제거, AutoFit."""
        try:
            wb = openpyxl.load_workbook(input_path)
            for ws in wb.worksheets:
                if not isinstance(ws, Worksheet):
                    continue
                try:
                    ws.print_area = None
                    for col in ws.columns:
                        max_length = 0
                        col_letter = get_column_letter(col[0].column)
                        for cell in col[:100]:
                            try:
                                if cell.value:
                                    max_length = max(max_length, len(str(cell.value)))
                            except Exception:
                                pass
                        ws.column_dimensions[col_letter].width = min(max_length + 2, 50)
                    ws.page_setup.paperSize = 66
                    ws.page_setup.orientation = ws.ORIENTATION_LANDSCAPE
                    ws.page_setup.fitToPage = True
                    ws.page_setup.fitToWidth = 1
                    ws.page_setup.fitToHeight = False
                    ws.page_margins = PageMargins(left=0, right=0, top=0, bottom=0, header=0, footer=0)
                except Exception:
                    continue
            wb.save(output_path)
            return True
        except InvalidFileException:
            shutil.copy2(input_path, output_path)
            return False
        except Exception as e:
            logger.warning(f"엑셀 레이아웃 최적화 실패: {e}")
            shutil.copy2(input_path, output_path)
            return False

    @classmethod
    def _convert_excel_to_pdf_with_sheet_map(cls, input_path, output_dir):
        """Excel 시트별 개별 PDF 변환 → 합친 PDF + 시트 페이지 매핑 반환.
        Returns: (merged_pdf_path, sheet_map)
          sheet_map = [{"name": "시트명", "page_start": 1, "page_end": 3}, ...]
        """
        if not os.path.exists(output_dir): os.makedirs(output_dir)
        cmd_exe = cls.get_libreoffice_cmd()
        if not cmd_exe: return None, []

        try:
            wb_check = openpyxl.load_workbook(input_path, read_only=True, data_only=True)
            sheet_names = wb_check.sheetnames
            wb_check.close()
        except Exception:
            return cls.convert_to_pdf(input_path, output_dir), []

        abs_output_dir = os.path.abspath(output_dir)
        temp_id = uuid.uuid4().hex
        sheet_pdfs = []
        sheet_map = []
        current_page = 1

        for sheet_name in sheet_names:
            try:
                wb = openpyxl.load_workbook(input_path)
                for sn in [s for s in wb.sheetnames if s != sheet_name]:
                    del wb[sn]
                safe_name = re.sub(r'[\\/:*?"<>|]', '_', sheet_name)
                temp_xlsx = os.path.join(abs_output_dir, f"temp_{temp_id}_{safe_name}.xlsx")
                opt_xlsx = os.path.join(abs_output_dir, f"opt_{temp_id}_{safe_name}.xlsx")
                wb.save(temp_xlsx)
                wb.close()
            except Exception as e:
                logger.warning(f"시트 {sheet_name} xlsx 생성 실패: {e}")
                continue

            cls._optimize_excel_layout(temp_xlsx, opt_xlsx)

            sheet_pdf = os.path.join(abs_output_dir, f"sheet_{temp_id}_{safe_name}.pdf")
            generated = os.path.join(abs_output_dir, f"opt_{temp_id}_{safe_name}.pdf")
            cmd = [cmd_exe, "--headless", "--convert-to", "pdf", "--outdir", abs_output_dir, opt_xlsx]
            try:
                startupinfo = None
                if platform.system() == "Windows":
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               startupinfo=startupinfo, timeout=90)
                if os.path.exists(generated):
                    os.rename(generated, sheet_pdf)
                    doc = fitz.open(sheet_pdf)
                    n_pages = len(doc)
                    doc.close()
                    sheet_map.append({"name": sheet_name, "page_start": current_page, "page_end": current_page + n_pages - 1})
                    current_page += n_pages
                    sheet_pdfs.append(sheet_pdf)
            except Exception as e:
                logger.warning(f"시트 {sheet_name} PDF 변환 실패: {e}")
            finally:
                for f in [temp_xlsx, opt_xlsx]:
                    if os.path.exists(f): os.remove(f)

        if not sheet_pdfs:
            return None, []

        name = os.path.splitext(os.path.basename(input_path))[0]
        merged_pdf_path = os.path.join(abs_output_dir, f"{name}.pdf")
        try:
            merged = fitz.open()
            for sp in sheet_pdfs:
                src = fitz.open(sp)
                merged.insert_pdf(src)
                src.close()
            merged.save(merged_pdf_path)
            merged.close()
        except Exception as e:
            logger.error(f"PDF 합치기 실패: {e}")
            return None, []
        finally:
            for sp in sheet_pdfs:
                if os.path.exists(sp): os.remove(sp)

        return merged_pdf_path, sheet_map

    @classmethod
    def convert_to_pdf(cls, input_path, output_dir):
        """LibreOffice를 사용해 문서를 PDF로 변환하고 결과 경로를 반환한다."""
        if not os.path.exists(output_dir): os.makedirs(output_dir)
        cmd_exe = cls.get_libreoffice_cmd()
        if not cmd_exe: return None

        filename = os.path.basename(input_path)
        name, ext = os.path.splitext(filename)
        temp_id = uuid.uuid4().hex
        abs_output_dir = os.path.abspath(output_dir)
        safe_input_path = os.path.join(abs_output_dir, f"temp_{temp_id}{ext}")

        if ext.lower() == '.xlsx':
            cls._optimize_excel_layout(input_path, safe_input_path)
        else:
            shutil.copy2(input_path, safe_input_path)

        cmd = [cmd_exe, "--headless", "--convert-to", "pdf", "--outdir", abs_output_dir, safe_input_path]
        generated_temp_pdf = os.path.join(abs_output_dir, f"temp_{temp_id}.pdf")
        final_pdf_path = os.path.join(abs_output_dir, f"{name}.pdf")

        try:
            startupinfo = None
            if platform.system() == "Windows":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startupinfo, timeout=90)
            if result.returncode != 0:
                logger.error(f"LibreOffice 변환 실패 (code {result.returncode}): {result.stderr.decode('utf-8', errors='replace')}")
                return None

            if os.path.exists(generated_temp_pdf):
                if os.path.exists(final_pdf_path): os.remove(final_pdf_path)
                os.rename(generated_temp_pdf, final_pdf_path)
                return final_pdf_path

            logger.error(f"LibreOffice 변환 후 PDF 없음: {generated_temp_pdf}")
            return None
        except Exception as e:
            logger.error(f"LibreOffice 변환 예외: {e}")
            return None
        finally:
            if os.path.exists(safe_input_path): os.remove(safe_input_path)
            if os.path.exists(generated_temp_pdf): os.remove(generated_temp_pdf)

    @classmethod
    def convert_hwp_to_pdf_via_odt(cls, input_path, output_dir):
        """HWP → ODT (hwp5odt) → PDF (LibreOffice) 2단계 변환. LibreOffice 직접 변환이 안 될 때 사용."""
        if not os.path.exists(output_dir): os.makedirs(output_dir)
        cmd_exe = cls.get_libreoffice_cmd()
        if not cmd_exe: return None

        filename = os.path.basename(input_path)
        name, _ = os.path.splitext(filename)
        temp_id = uuid.uuid4().hex
        abs_output_dir = os.path.abspath(output_dir)
        temp_odt_path = os.path.join(abs_output_dir, f"temp_{temp_id}.odt")
        final_pdf_path = os.path.join(abs_output_dir, f"{name}.pdf")

        try:
            result = subprocess.run(
                ['hwp5odt', '--output', temp_odt_path, input_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60
            )
            if result.returncode != 0 or not os.path.exists(temp_odt_path):
                logger.error(f"hwp5odt 변환 실패: {result.stderr.decode('utf-8', errors='replace')}")
                return None

            startupinfo = None
            if platform.system() == "Windows":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            result2 = subprocess.run(
                [cmd_exe, "--headless", "--convert-to", "pdf", "--outdir", abs_output_dir, temp_odt_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                startupinfo=startupinfo, timeout=90
            )
            if result2.returncode != 0:
                logger.error(f"ODT→PDF 변환 실패: {result2.stderr.decode('utf-8', errors='replace')}")
                return None

            odt_pdf = os.path.join(abs_output_dir, f"temp_{temp_id}.pdf")
            if os.path.exists(odt_pdf):
                if os.path.exists(final_pdf_path): os.remove(final_pdf_path)
                os.rename(odt_pdf, final_pdf_path)
                return final_pdf_path
            return None
        except Exception as e:
            logger.error(f"HWP→ODT→PDF 변환 예외: {e}")
            return None
        finally:
            if os.path.exists(temp_odt_path): os.remove(temp_odt_path)

    @staticmethod
    def _auto_click_hwp_popup():
        """HWP COM 변환 중 나타나는 보안 팝업을 백그라운드에서 자동으로 닫는다."""
        try:
            from pywinauto import Desktop
            import threading
            import time

            def clicker():
                for _ in range(30):
                    try:
                        dlg = Desktop(backend="win32").window(title_re=".*한글.*|.*보안.*", visible_only=True)
                        if dlg.exists():
                            btn = dlg.child_window(title_re=".*모두 허용.*|.*허용.*", class_name="Button")
                            if btn.exists():
                                btn.click()
                                break
                    except Exception: pass
                    time.sleep(0.5)

            t = threading.Thread(target=clicker)
            t.daemon = True
            t.start()
        except ImportError:
            logger.warning("pywinauto 패키지가 없습니다.")

    @classmethod
    def convert_hwp_to_pdf_win32(cls, input_path, output_dir):
        """HWP/HWPX → PDF 변환. HWP COM은 스레드에서 동작 안 하므로 별도 subprocess로 실행."""
        if platform.system() != "Windows": return None

        filename = os.path.basename(input_path)
        name, _ = os.path.splitext(filename)
        temp_id = uuid.uuid4().hex
        abs_output_dir = os.path.abspath(output_dir)
        temp_pdf_path = os.path.join(abs_output_dir, f"temp_{temp_id}.pdf")
        final_pdf_path = os.path.join(abs_output_dir, f"{name}.pdf")

        helper_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hwp_to_pdf.py")

        try:
            result = subprocess.run(
                [sys.executable, helper_script, input_path, temp_pdf_path],
                timeout=120, capture_output=True, text=True
            )
            if result.returncode != 0:
                logger.error(f"HWP 변환 subprocess 실패: {result.stderr.strip()}")
                return None

            if os.path.exists(temp_pdf_path):
                if os.path.exists(final_pdf_path): os.remove(final_pdf_path)
                os.rename(temp_pdf_path, final_pdf_path)
                return final_pdf_path
            return None
        except Exception as e:
            logger.error(f"HWP 변환 subprocess 예외: {e}")
            return None

    @staticmethod
    def json_to_markdown(structured_json_str: str) -> str:
        """_structured.json 문자열 → Markdown 문자열 변환"""
        try:
            data = json.loads(structured_json_str)
        except json.JSONDecodeError:
            return structured_json_str
        elements = data.get("elements", [])
        types = [e.get("type", "text") for e in elements]
        has_toc = "toc_entry" in types
        has_heading = any(t in ("heading_1", "heading_2", "heading_3") for t in types)
        lines = []
        if has_toc and not has_heading:
            lines.append("# Table of Contents")
            lines.append("")
        for elem in elements:
            etype = elem.get("type", "text")
            content = (elem.get("content") or "").strip()
            caption = (elem.get("caption") or "").strip()
            description = (elem.get("description") or "").strip()

            if etype in ("heading_2", "heading_3"):
                mid_period = re.search(r'[a-zA-Z]\.\s+[A-Z]', content)
                if mid_period and len(content.split()) > 10:
                    split_pos = content.index(mid_period.group()[2:], mid_period.start())
                    bold_part = content[:mid_period.start() + 1]
                    rest_part = content[mid_period.start() + 2:].strip()
                    content = f"**{bold_part}** {rest_part}"
                    etype = "text"
                elif (content.endswith('.') and len(content.split()) <= 8
                      and not re.match(r'^[\d.]+\s', content)
                      and not re.match(r'^(Figure|Fig|Table|Eq)\b', content, re.IGNORECASE)):
                    content = f"**{content}**"
                    etype = "text"

            if etype == "heading_1":
                lines.append(f"# {content}")
            elif etype == "heading_2":
                lines.append(f"## {content}")
            elif etype == "heading_3":
                lines.append(f"### {content}")
            elif etype == "toc_entry":
                parts = content.split("::", 1)
                if len(parts) == 2:
                    lines.append(f"- {parts[0].strip()} ··· {parts[1].strip()}")
                else:
                    lines.append(f"- {content}")
            elif etype == "table":
                if caption:
                    lines.append(caption)
                if content:
                    lines.append(content)
            elif etype == "figure":
                if caption:
                    lines.append(f"*{caption}*")
                if description:
                    lines.append(f"> {description}")
                if content:
                    lines.append(content)
            elif etype == "footnote":
                if content:
                    lines.append(f"---\n> {content}")
            else:
                if content:
                    lines.append(content)
            lines.append("")
        return "\n".join(lines).strip()

    @classmethod
    def process_and_save(cls, file_path, base_output_dir, api_key=None, output_format="json", model_name="Qwen/Qwen3-VL-8B-Instruct", progress_callback=None, chunk_strategies=None):
        """문서를 파싱·구조화하여 페이지별 파일을 출력 디렉터리에 저장한다."""
        filename = os.path.basename(file_path)
        name, ext = os.path.splitext(filename)
        ext = ext.lower()
        
        doc_output_dir = os.path.join(base_output_dir, name)
        if os.path.exists(doc_output_dir): shutil.rmtree(doc_output_dir)
        os.makedirs(doc_output_dir)

        def set_progress(msg, percent):
            logger.info(msg)
            if progress_callback: progress_callback({"msg": msg, "percent": percent})

        set_progress("문서 파싱 준비 중...", 2)
        temp_pdf_dir = os.path.join(base_output_dir, "temp_pdf")
        if not os.path.exists(temp_pdf_dir): os.makedirs(temp_pdf_dir)
        excel_sheet_map = []

        if ext in ['.hwp', '.hwpx']:
            set_progress(f"{ext.upper()} PDF 변환 중 (한글 COM)...", 5)
            pdf_path = cls.convert_hwp_to_pdf_win32(file_path, temp_pdf_dir)

            if pdf_path:
                try:
                    doc = fitz.open(pdf_path)
                    out_page = 0
                    mat = fitz.Matrix(100 / 72, 100 / 72)
                    for i in range(len(doc)):
                        page = doc[i]
                        w, h = page.rect.width, page.rect.height
                        is_two_up = w > h
                        halves = (
                            [(0, 0, w / 2, h), (w / 2, 0, w, h)] if is_two_up
                            else [(0, 0, w, h)]
                        )
                        for x0, y0, x1, y1 in halves:
                            out_page += 1
                            pnum = str(out_page).zfill(4)
                            clip = fitz.Rect(x0, y0, x1, y1)
                            pix = page.get_pixmap(matrix=mat, clip=clip)
                            pix.save(os.path.join(doc_output_dir, f"page_{pnum}.png"))
                            words = page.get_text("words")
                            region_words = [
                                wd for wd in words
                                if wd[0] >= x0 - 5 and wd[2] <= x1 + 5
                            ]
                            text = " ".join(
                                wd[4] for wd in sorted(region_words, key=lambda r: (r[1], r[0]))
                            )
                            cleaned_text = HWPTextExtractor.text_preprocessing(text.strip())
                            with open(os.path.join(doc_output_dir, f"page_{pnum}.txt"), "w", encoding="utf-8") as f:
                                f.write(cleaned_text)
                    doc.close()
                    set_progress(f"{ext.upper()} 파싱 완료 ({out_page}쪽)", 20)
                except Exception as e:
                    raise Exception(f"{ext.upper()} PDF 변환 후 처리 실패: {e}")
            else:
                raise Exception(f"{ext.upper()} → PDF 변환 실패. 한글(HWP) 프로그램이 설치되어 있는지 확인하세요.")

        else:
            set_progress("일반 문서 시각 레이아웃 생성 중...", 10)
            if ext == '.pdf':
                pdf_path = file_path
            elif ext == '.xlsx':
                pdf_path, excel_sheet_map = cls._convert_excel_to_pdf_with_sheet_map(file_path, temp_pdf_dir)
            else:
                pdf_path = cls.convert_to_pdf(file_path, temp_pdf_dir)

            if pdf_path:
                try:
                    doc = fitz.open(pdf_path)
                    total_pages = len(doc)
                    for i in range(total_pages):
                        pnum = str(i + 1).zfill(4)
                        pix = doc[i].get_pixmap(dpi=100)
                        pix.save(os.path.join(doc_output_dir, f"page_{pnum}.png"))

                        raw_text = doc[i].get_text().strip()
                        cleaned_text = HWPTextExtractor.text_preprocessing(raw_text)
                        with open(os.path.join(doc_output_dir, f"page_{pnum}.txt"), "w", encoding="utf-8") as f:
                            f.write(cleaned_text)
                    set_progress(f"문서 파싱 완료 ({total_pages}쪽)", 20)
                    doc.close()
                except Exception as e:
                    set_progress(f"❌ 문서 파싱 에러: {e}", 20)

        page_files = sorted(f for f in os.listdir(doc_output_dir) if f.startswith("page_") and f.endswith(".txt"))
        total_vlm_pages = len(page_files)

        if not page_files: return set_progress("분석할 텍스트가 없어 종료합니다.", 100)
        if not api_key or len(api_key) < 10 or "여기에" in api_key: raise Exception("API 키가 누락되었거나 유효하지 않습니다.")

        set_progress("문서 메타데이터 추출 중...", 22)
        meta_txts = [os.path.join(doc_output_dir, f"page_{str(i).zfill(4)}.txt") for i in range(1, 3)]
        meta_imgs = [os.path.join(doc_output_dir, f"page_{str(i).zfill(4)}.png") for i in range(1, 3)]
        metadata = VLMProcessor.extract_metadata(meta_txts, meta_imgs, api_key, model_name)
        metadata["source_file"] = filename
        if excel_sheet_map:
            metadata["sheets"] = excel_sheet_map
        with open(os.path.join(doc_output_dir, "metadata.json"), "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        set_progress(f"🤖 AI 분석 대기 중 (총 {total_vlm_pages}페이지)...", 25)

        for idx, txt_file in enumerate(page_files):
            start_percent = 25 + int(((idx) / total_vlm_pages) * 75)
            set_progress(f"⏳ AI 모델 분석 중... ({idx + 1}/{total_vlm_pages} 쪽)", start_percent)
            
            stem = txt_file[:-4]
            txt_path = os.path.join(doc_output_dir, txt_file)
            img_path = os.path.join(doc_output_dir, f"{stem}.png")
            if not os.path.exists(img_path): img_path = None

            vlm_fmt = "json" if output_format.lower() == "markdown" else output_format
            structured_data = VLMProcessor.extract_structure(
                txt_path=txt_path, img_path=img_path, api_key=api_key,
                output_format=vlm_fmt, model_name=model_name
            )

            if not structured_data:
                logger.warning(f"{stem} AI 분석 실패, 건너뜀")
                set_progress(f"⚠️ {stem} 분석 실패 (건너뜀)", 25 + int(((idx + 1) / total_vlm_pages) * 75))
                continue

            if vlm_fmt == "json":
                try:
                    parsed = json.loads(structured_data)
                    try:
                        file_page_num = int(stem.split("_")[1])
                        parsed["page_number"] = file_page_num
                    except (IndexError, ValueError):
                        pass
                    seen_contents = set()
                    deduped = []
                    for elem in parsed.get("elements", []):
                        content_only = elem.get("content", "").strip()
                        if content_only and content_only in seen_contents:
                            logger.debug(f"중복 element 제거: {content_only[:40]}...")
                            continue
                        if content_only:
                            seen_contents.add(content_only)
                        deduped.append(elem)
                    parsed["elements"] = deduped
                    structured_data = json.dumps(parsed, ensure_ascii=False, indent=4)
                except Exception as e:
                    logger.warning(f"JSON 후처리 실패 ({stem}): {e}")

            if output_format.lower() == "markdown":
                with open(os.path.join(doc_output_dir, f"{stem}_structured.json"), "w", encoding="utf-8") as f:
                    f.write(structured_data)
                md_data = DocumentProcessor.json_to_markdown(structured_data)
                with open(os.path.join(doc_output_dir, f"{stem}_structured.md"), "w", encoding="utf-8") as f:
                    f.write(md_data)
            else:
                ext_str = output_format.lower()
                with open(os.path.join(doc_output_dir, f"{stem}_structured.{ext_str}"), "w", encoding="utf-8") as f:
                    f.write(structured_data)
                
            set_progress(f"✅ {idx + 1}쪽 분석 완료!", 25 + int(((idx + 1) / total_vlm_pages) * 75))

        if output_format.lower() in ("json", "markdown"):
            toc_entries = []
            toc_ended = False
            for fname in sorted(f for f in os.listdir(doc_output_dir) if f.endswith("_structured.json")):
                if toc_ended:
                    break
                fpath = os.path.join(doc_output_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    page_has_toc = False
                    for elem in data.get("elements", []):
                        if elem.get("type") == "toc_entry":
                            page_has_toc = True
                            raw = elem.get("content", "")
                            if "::" in raw:
                                title, _, pg = raw.rpartition("::")
                                try:
                                    toc_entries.append({"title": title.strip(), "page": int(pg.strip())})
                                except ValueError:
                                    toc_entries.append({"title": title.strip(), "page": pg.strip()})
                            else:
                                toc_entries.append({"title": raw.strip(), "page": None})
                    if not page_has_toc and toc_entries:
                        toc_ended = True
                except Exception as e:
                    logger.warning(f"TOC 수집 중 {fname} 읽기 실패: {e}")

            if toc_entries:
                meta_path = os.path.join(doc_output_dir, "metadata.json")
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        metadata = json.load(f)
                except Exception:
                    metadata = {}
                metadata["toc"] = toc_entries
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(metadata, f, ensure_ascii=False, indent=2)
                logger.info(f"TOC {len(toc_entries)}개 항목 수집 완료")

        if chunk_strategies and output_format.lower() == "json":
            try:
                from chunker import chunk_document
                set_progress("📦 청킹 중...", 98)
                chunk_document(doc_output_dir, strategies=chunk_strategies)
            except Exception as e:
                logger.error(f"청킹 오류: {e}")

        if os.path.exists(temp_pdf_dir):
            shutil.rmtree(temp_pdf_dir, ignore_errors=True)

        set_progress("🎉 모든 구조화 완료!", 100)