import os
import io
import json
import hmac
import uuid
import threading
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
import zipfile
import re
from datetime import datetime
from urllib.parse import quote
from flask import Flask, render_template, request, jsonify, send_file, send_from_directory, abort
from utils import DocumentProcessor

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = os.environ.get("UPLOAD_FOLDER", './documents')
app.config['OUTPUT_FOLDER'] = os.environ.get("OUTPUT_FOLDER", './processed')
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get("MAX_UPLOAD_MB", "512")) * 1024 * 1024
HISTORY_FILE = 'history.json'
ALLOWED_FORMATS = {"json", "xml", "markdown"}
ALLOWED_EXTENSIONS = {
    ".pdf", ".hwp", ".hwpx", ".docx", ".doc", ".pptx", ".ppt",
    ".xlsx", ".xls", ".odt", ".rtf", ".txt", ".csv", ".eml",
}

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

VLM_BASE_URL = os.environ.get("VLM_BASE_URL", "http://localhost:8000/v1")
API_KEY = os.environ.get("VLM_API_KEY", "local-vllm-noauth-key")
VIEWER_AUTH_USER = os.environ.get("VIEWER_AUTH_USER", "")
VIEWER_AUTH_PASSWORD = os.environ.get("VIEWER_AUTH_PASSWORD", "")
VIEWER_HOST = os.environ.get("VIEWER_HOST", "127.0.0.1")
ALLOW_UNAUTHENTICATED_REMOTE = os.environ.get(
    "ALLOW_UNAUTHENTICATED_REMOTE", ""
).lower() in {"1", "true", "yes", "on"}

if bool(VIEWER_AUTH_USER) != bool(VIEWER_AUTH_PASSWORD):
    raise RuntimeError(
        "VIEWER_AUTH_USER and VIEWER_AUTH_PASSWORD must be configured together"
    )

tasks_progress = {}
history_lock = threading.Lock()


@app.before_request
def require_viewer_auth():
    """Protect every web/API route when viewer credentials are configured."""
    if not VIEWER_AUTH_USER:
        return None
    auth = request.authorization
    valid = bool(
        auth
        and hmac.compare_digest(auth.username or "", VIEWER_AUTH_USER)
        and hmac.compare_digest(auth.password or "", VIEWER_AUTH_PASSWORD)
    )
    if valid:
        return None
    response = jsonify({"error": "Authentication required"})
    response.status_code = 401
    response.headers["WWW-Authenticate"] = (
        'Basic realm="ModuDoc Viewer", charset="UTF-8"'
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.after_request
def disable_document_caching(response):
    """Parsed documents and page images must not be retained by shared caches."""
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; connect-src 'self'; "
        "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
        "img-src 'self' data:; object-src 'none'; "
        "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'"
    )
    return response


def validate_viewer_exposure(host):
    normalized = str(host or "").strip().lower().strip("[]")
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    if (
        normalized not in local_hosts
        and not VIEWER_AUTH_USER
        and not ALLOW_UNAUTHENTICATED_REMOTE
    ):
        raise RuntimeError(
            "Refusing an unauthenticated non-loopback bind. Configure "
            "VIEWER_AUTH_USER and VIEWER_AUTH_PASSWORD, or explicitly set "
            "ALLOW_UNAUTHENTICATED_REMOTE=1 for a public sample-only instance."
        )

def get_safe_filename(filename):
    basename = os.path.basename(filename or "")
    basename = re.sub(r'[\x00-\x1f\x7f]', '', basename)
    basename = re.sub(r'[\/\\\:\*\?\"\<\>\|]', '_', basename)
    basename = re.sub(r'\s+', ' ', basename).strip(' ._')
    return basename[:180]

def make_output_name(filename, task_id, idx):
    stem = os.path.splitext(filename)[0]
    stem = re.sub(r'[^\w가-힣.-]+', '_', stem, flags=re.UNICODE).strip('._') or "document"
    return f"{stem}__{task_id[:8]}__{idx:03d}"

def processed_entries(task):
    entries = []
    for item in task.get('processed', []):
        if isinstance(item, dict):
            entries.append(item)
        else:
            name = str(item)
            entries.append({
                "filename": name,
                "display": name,
                "output_dir": os.path.splitext(name)[0],
            })
    return entries

def load_history():
    if os.path.exists(HISTORY_FILE) and os.path.getsize(HISTORY_FILE) > 0:
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f: return json.load(f)
        except: return []
    return []

def save_history(history):
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=4)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/history')
def history_page():
    return render_template('history.html', history=load_history())

@app.route('/api/progress/<task_id>')
def get_progress(task_id):
    if task_id not in tasks_progress:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(tasks_progress[task_id])

@app.route('/api/process', methods=['POST'])
def process_files():
    if not API_KEY or "여기에_" in API_KEY:
        return jsonify({"error": "VLM 엔드포인트 설정이 필요합니다. VLM_BASE_URL(기본 http://localhost:8000/v1)을 확인하세요."}), 400

    if 'files' not in request.files:
        return jsonify({"error": "파일이 없습니다."}), 400
        
    output_format = (request.form.get('format', 'json') or 'json').lower()
    if output_format not in ALLOWED_FORMATS:
        return jsonify({"error": "지원하지 않는 출력 포맷입니다."}), 400
    model_name = request.form.get('model', 'Qwen/Qwen3-VL-30B-A3B-Instruct')
    chunk_strategies = request.form.getlist('chunk')
    try:
        concurrency = max(1, min(16, int(request.form.get('concurrency', 3))))
    except (TypeError, ValueError):
        concurrency = 3
    task_id = str(uuid.uuid4())
    tasks_progress[task_id] = {"progress": 0, "status": "업로드 중...", "is_done": False, "error": None, "warnings": [], "processed": [], "docs": {}}

    saved_files = []
    task_upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], task_id)
    os.makedirs(task_upload_dir, exist_ok=True)
    seen_names = {}
    for idx, file in enumerate(request.files.getlist('files'), start=1):
        if file.filename != '':
            filename = get_safe_filename(file.filename)
            if not filename:
                tasks_progress.pop(task_id, None)
                shutil.rmtree(task_upload_dir, ignore_errors=True)
                return jsonify({"error": "유효하지 않은 파일명입니다."}), 400
            ext = os.path.splitext(filename)[1].lower()
            if ext not in ALLOWED_EXTENSIONS:
                tasks_progress.pop(task_id, None)
                shutil.rmtree(task_upload_dir, ignore_errors=True)
                return jsonify({"error": f"지원하지 않는 파일 형식입니다: {filename}"}), 400
            seen_names[filename] = seen_names.get(filename, 0) + 1
            display_name = filename if seen_names[filename] == 1 else f"{filename} ({seen_names[filename]})"
            stored_name = f"{idx:03d}_{filename}"
            file_path = os.path.join(task_upload_dir, stored_name)
            try:
                file.save(file_path)
            except Exception as e:
                tasks_progress.pop(task_id, None)
                shutil.rmtree(task_upload_dir, ignore_errors=True)
                return jsonify({"error": f"파일 저장 실패: {filename} ({e})"}), 500
            saved_files.append({
                "path": file_path,
                "filename": filename,
                "display": display_name,
                "output_name": make_output_name(filename, task_id, idx),
            })

    if not saved_files:
        tasks_progress.pop(task_id, None)
        shutil.rmtree(task_upload_dir, ignore_errors=True)
        return jsonify({"error": "유효한 파일이 없습니다."}), 400

    def background_worker(t_id, files_info, out_fmt, mod_name, chunk_strats, conc):
        total_files = len(files_info)
        doc_progress = {info["display"]: 0 for info in files_info}
        doc_status = {info["display"]: "대기 중..." for info in files_info}
        lock = threading.Lock()

        def update_overall():
            avg = int(sum(doc_progress.values()) / total_files)
            tasks_progress[t_id]["progress"] = avg
            tasks_progress[t_id]["docs"] = {
                f: {"progress": doc_progress[f], "status": doc_status[f]}
                for f in doc_progress
            }

        def make_progress_cb(f_name):
            def cb(info):
                with lock:
                    doc_progress[f_name] = info["percent"]
                    doc_status[f_name] = info["msg"]
                    update_overall()
            return cb

        def process_one(info):
            output_dir = DocumentProcessor.process_and_save(
                file_path=info["path"], base_output_dir=app.config['OUTPUT_FOLDER'],
                api_key=API_KEY, output_format=out_fmt, model_name=mod_name,
                progress_callback=make_progress_cb(info["display"]),
                chunk_strategies=chunk_strats or None,
                output_name=info["output_name"],
                source_filename=info["filename"],
            )
            return {
                "filename": info["filename"],
                "display": info["display"],
                "output_dir": os.path.basename(output_dir),
            }

        try:
            processed_docs = []
            errors = []
            max_workers = max(1, min(total_files, conc))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(process_one, info): info["display"]
                           for info in files_info}
                for future in as_completed(futures):
                    f_name = futures[future]
                    try:
                        processed_docs.append(future.result())
                    except Exception as e:
                        errors.append(f"[{f_name}] {e}")
                        with lock:
                            doc_progress[f_name] = 100
                            doc_status[f_name] = f"실패: {e}"
                            update_overall()

            if processed_docs:
                with history_lock:
                    hist = load_history()
                    hist.insert(0, {"date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "files": [d["display"] for d in processed_docs], "format": out_fmt.upper(), "model": mod_name})
                    save_history(hist)

            tasks_progress[t_id]["progress"] = 100
            if errors and not processed_docs:
                tasks_progress[t_id]["status"] = "작업 실패"
                tasks_progress[t_id]["error"] = " / ".join(errors)
            elif errors:
                tasks_progress[t_id]["status"] = "일부 작업 실패"
                tasks_progress[t_id]["warnings"] = errors
            else:
                tasks_progress[t_id]["status"] = "모든 작업 완료!"
            tasks_progress[t_id]["processed"] = processed_docs
        except Exception as e:
            tasks_progress[t_id]["error"] = str(e)
        finally:
            if files_info:
                upload_dir = os.path.dirname(files_info[0]["path"])
                if os.path.basename(upload_dir) == t_id:
                    shutil.rmtree(upload_dir, ignore_errors=True)
            tasks_progress[t_id]["is_done"] = True

    threading.Thread(target=background_worker, args=(task_id, saved_files, output_format, model_name, chunk_strategies, concurrency)).start()
    return jsonify({"message": "작업 시작됨", "task_id": task_id})

@app.route('/api/download/<task_id>')
def download_result(task_id):
    task = tasks_progress.get(task_id)
    if not task or not task.get('processed'):
        return "다운로드할 파일이 없습니다.", 404
        
    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for entry in processed_entries(task):
            doc_dir_name = entry["output_dir"]
            doc_dir_path = os.path.join(app.config['OUTPUT_FOLDER'], doc_dir_name)
            
            if os.path.exists(doc_dir_path):
                for root, dirs, files in os.walk(doc_dir_path):
                    for file in files:
                        f_path = os.path.join(root, file)
                        arcname = os.path.join(doc_dir_name, os.path.relpath(f_path, doc_dir_path))
                        zf.write(f_path, arcname)
                        
    memory_file.seek(0)
    return send_file(memory_file, download_name="parsed_result.zip", as_attachment=True)

@app.route('/api/view/<task_id>')
def view_result(task_id):
    task = tasks_progress.get(task_id)
    if not task or not task.get('processed'):
        return jsonify({"error": "결과를 찾을 수 없습니다."}), 404

    results = {}
    for entry in processed_entries(task):
        doc_dir_name = entry["output_dir"]
        display = entry.get("display") or entry.get("filename") or doc_dir_name
        doc_dir_path = os.path.join(app.config['OUTPUT_FOLDER'], doc_dir_name)

        if os.path.exists(doc_dir_path):
            for root, dirs, files in os.walk(doc_dir_path):
                for file in files:
                    if file.endswith('_structured.json') or file.endswith('_structured.xml') or file.endswith('_structured.md'):
                        f_path = os.path.join(root, file)
                        with open(f_path, 'r', encoding='utf-8') as f:
                            results[f"{display} / {file}"] = f.read()
                            
    return jsonify(results)

# ---- 원본 ↔ 파싱결과 비교 뷰어 ----
def _doc_dir(name):
    """OUTPUT_FOLDER 하위의 안전한 문서 폴더 경로(경로탈출 차단)."""
    safe = os.path.basename(name or "")
    d = os.path.join(app.config['OUTPUT_FOLDER'], safe)
    if not safe or safe != name or safe in {".", ".."} or not os.path.isdir(d):
        abort(404)
    return d

def _page_no(fname):
    m = re.search(r'(\d+)', fname)
    return int(m.group(1)) if m else 0

def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]

def _load_json_file(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default

def _structured_payload(data, fallback_page):
    """Return a stable page payload for both legacy list JSON and newer object JSON."""
    if isinstance(data, dict):
        elements = data.get('elements', [])
        page_no = data.get('page_number') or fallback_page
        warnings = _as_list(data.get('warnings')) + _as_list(data.get('warning'))
        low_confidence = bool(data.get('low_confidence'))
        raw = data
    elif isinstance(data, list):
        elements = data
        page_no = fallback_page
        warnings = []
        low_confidence = False
        raw = data
    else:
        elements = []
        page_no = fallback_page
        warnings = ["구조화 JSON 형식을 읽을 수 없습니다."]
        low_confidence = True
        raw = data
    return {
        "elements": elements if isinstance(elements, list) else [],
        "page": page_no,
        "warnings": [str(w) for w in warnings if w],
        "low_confidence": low_confidence,
        "raw": raw,
    }

def _count_items(items):
    counts = {}
    for item in items:
        key = str(item or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts

def _element_summary(elements):
    types, sources, issues, confidences = [], [], [], []
    for el in elements:
        if not isinstance(el, dict):
            continue
        types.append(el.get("type") or "text")
        sources.append(el.get("_source") or "unknown")
        issues.extend(_as_list(el.get("_issues")))
        try:
            confidences.append(float(el.get("_confidence")))
        except (TypeError, ValueError):
            pass
    low_elements = len([c for c in confidences if c < 0.7])
    return {
        "elements": len(elements),
        "types": _count_items(types),
        "sources": _count_items(sources),
        "issues": _count_items(issues),
        "avg_confidence": round(sum(confidences) / len(confidences), 3) if confidences else None,
        "min_confidence": round(min(confidences), 3) if confidences else None,
        "low_confidence_elements": low_elements,
    }

@app.route('/compare')
def compare_page():
    return render_template('compare.html')

@app.route('/api/docs')
def api_docs():
    """파싱 결과(_structured.json)가 있는 문서 목록."""
    root = app.config['OUTPUT_FOLDER']
    docs = []
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            structured_files = [f for f in os.listdir(d) if f.endswith('_structured.json')]
            n_pages = len(structured_files)
            if not n_pages:
                continue
            title = name
            meta = {}
            mp = os.path.join(d, 'metadata.json')
            if os.path.exists(mp):
                meta = _load_json_file(mp, {}) or {}
                title = meta.get('doc_title') or name
            stat = os.stat(d)
            docs.append({
                "name": name,
                "title": title,
                "pages": n_pages,
                "source_file": meta.get("source_file"),
                "updated_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "vlm_failed_pages": meta.get("vlm_failed_pages") or [],
                "low_confidence_pages": meta.get("low_confidence_pages") or [],
                "vlm_pages_total": meta.get("vlm_pages_total"),
                "vlm_pages_analyzed": meta.get("vlm_pages_analyzed"),
                "provenance_summary": meta.get("provenance_summary") or {},
            })
    return jsonify(docs)

@app.route('/api/doc/<name>')
def api_doc(name):
    """한 문서의 페이지별 [원본 이미지 URL + 파싱 요소]."""
    d = _doc_dir(name)
    mp = os.path.join(d, 'metadata.json')
    meta = _load_json_file(mp, {}) if os.path.exists(mp) else {}
    meta = meta or {}
    low_pages = set(int(p) for p in (meta.get("low_confidence_pages") or []) if str(p).isdigit())
    failed_pages = set(int(p) for p in (meta.get("vlm_failed_pages") or []) if str(p).isdigit())
    pages = []
    structured = sorted((x for x in os.listdir(d) if x.endswith('_structured.json')), key=_page_no)
    for fname in structured:
        stem = fname[:-len('_structured.json')]
        data = _load_json_file(os.path.join(d, fname), {})
        payload = _structured_payload(data, _page_no(fname))
        elements = payload["elements"]
        img = None
        for ext in ('.png', '.jpg', '.jpeg'):
            if os.path.exists(os.path.join(d, stem + ext)):
                img = f"/api/file/{quote(name, safe='')}/{quote(stem + ext, safe='')}"
                break
        page_no = int(payload["page"] or _page_no(fname))
        summary = _element_summary(elements)
        pages.append({
            "page": page_no,
            "image": img,
            "elements": elements,
            "raw": payload["raw"],
            "warnings": payload["warnings"],
            "low_confidence": payload["low_confidence"] or page_no in low_pages,
            "failed": page_no in failed_pages,
            "summary": summary,
            "has_table": bool(summary["types"].get("table")),
            "has_issues": bool(summary["issues"]),
            "json_file": fname,
        })
    chunks = []
    for strategy in ("toc", "tree", "page"):
        path = os.path.join(d, f"split_{strategy}.json")
        if os.path.exists(path):
            split_data = _load_json_file(path, [])
            chunks.append({
                "strategy": strategy,
                "count": len(split_data) if isinstance(split_data, list) else None,
            })
    return jsonify({"name": name, "metadata": meta, "pages": pages, "chunks": chunks})

@app.route('/api/file/<name>/<path:filename>')
def api_file(name, filename):
    """문서 폴더 안의 파일(원본 페이지 이미지 등) 제공."""
    d = _doc_dir(name)
    safe = os.path.basename(filename)
    if not safe or not os.path.exists(os.path.join(d, safe)):
        abort(404)
    return send_from_directory(d, safe)

if __name__ == '__main__':
    debug = os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes", "on"}
    port = int(os.environ.get("PORT", "5000"))
    validate_viewer_exposure(VIEWER_HOST)
    app.run(host=VIEWER_HOST, debug=debug, port=port)
