import os
import io
import json
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import zipfile
import re
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, send_from_directory, abort
from utils import DocumentProcessor

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = './documents'
app.config['OUTPUT_FOLDER'] = './processed'
HISTORY_FILE = 'history.json'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

VLM_BASE_URL = os.environ.get("VLM_BASE_URL", "http://localhost:8000/v1")
API_KEY = os.environ.get("VLM_API_KEY", "local-vllm-noauth-key")

tasks_progress = {}

def get_safe_filename(filename):
    basename = os.path.basename(filename)
    return re.sub(r'[\/\\\:\*\?\"\<\>\|]', '', basename)

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
        
    output_format = request.form.get('format', 'json')
    model_name = request.form.get('model', 'Qwen/Qwen3-VL-30B-A3B-Instruct')
    chunk_strategies = request.form.getlist('chunk')
    try:
        concurrency = max(1, min(16, int(request.form.get('concurrency', 3))))
    except (TypeError, ValueError):
        concurrency = 3
    task_id = str(uuid.uuid4())
    tasks_progress[task_id] = {"progress": 0, "status": "업로드 중...", "is_done": False, "error": None, "processed": [], "docs": {}}

    saved_files = []
    for file in request.files.getlist('files'):
        if file.filename != '':
            filename = get_safe_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            saved_files.append((file_path, filename))

    if not saved_files:
        tasks_progress.pop(task_id, None)
        return jsonify({"error": "유효한 파일이 없습니다."}), 400

    def background_worker(t_id, files_info, out_fmt, mod_name, chunk_strats, conc):
        total_files = len(files_info)
        doc_progress = {f_name: 0 for _, f_name in files_info}
        doc_status = {f_name: "대기 중..." for _, f_name in files_info}
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

        def process_one(f_path, f_name):
            DocumentProcessor.process_and_save(
                file_path=f_path, base_output_dir=app.config['OUTPUT_FOLDER'],
                api_key=API_KEY, output_format=out_fmt, model_name=mod_name,
                progress_callback=make_progress_cb(f_name),
                chunk_strategies=chunk_strats or None
            )
            return f_name

        try:
            processed_docs = []
            max_workers = max(1, min(total_files, conc))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(process_one, f_path, f_name): f_name
                           for f_path, f_name in files_info}
                for future in as_completed(futures):
                    f_name = futures[future]
                    try:
                        processed_docs.append(future.result())
                    except Exception as e:
                        tasks_progress[t_id]["error"] = f"[{f_name}] {e}"

            if processed_docs:
                hist = load_history()
                hist.insert(0, {"date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "files": processed_docs, "format": out_fmt.upper(), "model": mod_name})
                save_history(hist)

            tasks_progress[t_id]["progress"] = 100
            tasks_progress[t_id]["status"] = "모든 작업 완료!"
            tasks_progress[t_id]["processed"] = processed_docs
        except Exception as e:
            tasks_progress[t_id]["error"] = str(e)
        finally:
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
        for f_name in task['processed']:
            doc_dir_name = os.path.splitext(f_name)[0]
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
    for f_name in task['processed']:
        doc_dir_name = os.path.splitext(f_name)[0]
        doc_dir_path = os.path.join(app.config['OUTPUT_FOLDER'], doc_dir_name)

        if os.path.exists(doc_dir_path):
            for root, dirs, files in os.walk(doc_dir_path):
                for file in files:
                    if file.endswith('_structured.json') or file.endswith('_structured.xml') or file.endswith('_structured.md'):
                        f_path = os.path.join(root, file)
                        with open(f_path, 'r', encoding='utf-8') as f:
                            results[f"{doc_dir_name} / {file}"] = f.read()
                            
    return jsonify(results)

# ---- 원본 ↔ 파싱결과 비교 뷰어 ----
def _doc_dir(name):
    """OUTPUT_FOLDER 하위의 안전한 문서 폴더 경로(경로탈출 차단)."""
    safe = os.path.basename(name or "")
    d = os.path.join(app.config['OUTPUT_FOLDER'], safe)
    if not safe or not os.path.isdir(d):
        abort(404)
    return d

def _page_no(fname):
    m = re.search(r'(\d+)', fname)
    return int(m.group(1)) if m else 0

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
            n_pages = len([f for f in os.listdir(d) if f.endswith('_structured.json')])
            if not n_pages:
                continue
            title = name
            mp = os.path.join(d, 'metadata.json')
            if os.path.exists(mp):
                try:
                    with open(mp, encoding='utf-8') as f:
                        title = (json.load(f) or {}).get('doc_title') or name
                except Exception:
                    pass
            docs.append({"name": name, "title": title, "pages": n_pages})
    return jsonify(docs)

@app.route('/api/doc/<name>')
def api_doc(name):
    """한 문서의 페이지별 [원본 이미지 URL + 파싱 요소]."""
    d = _doc_dir(name)
    meta = {}
    mp = os.path.join(d, 'metadata.json')
    if os.path.exists(mp):
        try:
            with open(mp, encoding='utf-8') as f:
                meta = json.load(f) or {}
        except Exception:
            meta = {}
    pages = []
    structured = sorted((x for x in os.listdir(d) if x.endswith('_structured.json')), key=_page_no)
    for fname in structured:
        stem = fname[:-len('_structured.json')]
        try:
            with open(os.path.join(d, fname), encoding='utf-8') as fh:
                data = json.load(fh)
        except Exception:
            data = {}
        elements = data.get('elements', []) if isinstance(data, dict) else (data or [])
        img = None
        for ext in ('.png', '.jpg', '.jpeg'):
            if os.path.exists(os.path.join(d, stem + ext)):
                img = f"/api/file/{name}/{stem}{ext}"
                break
        pages.append({"page": _page_no(fname), "image": img, "elements": elements})
    chunks = [s for s in ("toc", "tree", "page") if os.path.exists(os.path.join(d, f"split_{s}.json"))]
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
    app.run(debug=True, port=5000)
