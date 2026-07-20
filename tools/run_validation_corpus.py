#!/usr/bin/env python3
"""Parse a fixed validation corpus and keep an incremental run manifest."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from utils import DocumentProcessor  # noqa: E402


SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".hwp",
    ".hwpx",
    ".eml",
    ".docx",
    ".doc",
    ".pptx",
    ".ppt",
    ".xlsx",
    ".xls",
    ".odt",
    ".rtf",
    ".txt",
    ".csv",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def collect_documents(inputs: list[str]) -> list[Path]:
    found: dict[str, Path] = {}
    for raw in inputs:
        path = Path(raw).expanduser().resolve()
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            found[str(path)] = path
        elif path.is_dir():
            for candidate in path.rglob("*"):
                if candidate.is_file() and candidate.suffix.lower() in SUPPORTED_EXTENSIONS:
                    found[str(candidate.resolve())] = candidate.resolve()
        else:
            raise FileNotFoundError(f"validation input not found or unsupported: {path}")
    return sorted(found.values(), key=lambda item: (item.name.casefold(), str(item)))


def write_manifest(path: Path, manifest: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default="Qwen/Qwen3-VL-30B-A3B-Instruct")
    parser.add_argument("--expected-documents", type=int)
    parser.add_argument("--chunk-strategies", nargs="+", default=["page", "toc", "tree"])
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    documents = collect_documents(args.inputs)
    if args.expected_documents is not None and len(documents) != args.expected_documents:
        print(
            f"expected {args.expected_documents} documents, found {len(documents)}",
            file=sys.stderr,
        )
        return 2

    output = Path(args.output).expanduser().resolve()
    if output.exists():
        if not args.force:
            print(f"output already exists: {output}", file=sys.stderr)
            return 2
        shutil.rmtree(output)
    output.mkdir(parents=True)

    manifest_path = output / "parse_manifest.json"
    manifest = {
        "started_at": utc_now(),
        "completed_at": None,
        "model": args.model,
        "vlm_base_url": os.environ.get("VLM_BASE_URL"),
        "expected_documents": args.expected_documents,
        "input_count": len(documents),
        "output": str(output),
        "documents": [],
    }
    write_manifest(manifest_path, manifest)

    failures = 0
    for index, document in enumerate(documents, 1):
        started = time.monotonic()
        record = {
            "index": index,
            "input": str(document),
            "name": document.name,
            "status": "running",
            "output_dir": None,
            "structured_pages": 0,
            "seconds": None,
            "error": None,
        }
        manifest["documents"].append(record)
        write_manifest(manifest_path, manifest)
        print(f"[{index}/{len(documents)}] START {document.name}", flush=True)

        def progress(event: dict) -> None:
            message = event.get("msg", "") if isinstance(event, dict) else str(event)
            percent = event.get("percent", "?") if isinstance(event, dict) else "?"
            print(f"[{index}/{len(documents)}] {percent}% {message}", flush=True)

        try:
            doc_output = Path(
                DocumentProcessor.process_and_save(
                    file_path=str(document),
                    base_output_dir=str(output),
                    api_key=os.environ.get("VLM_API_KEY", "local-vllm-noauth-key"),
                    output_format="json",
                    model_name=args.model,
                    progress_callback=progress,
                    chunk_strategies=args.chunk_strategies,
                )
            )
            record["status"] = "success"
            record["output_dir"] = str(doc_output)
            record["structured_pages"] = len(list(doc_output.glob("page_*_structured.json")))
        except Exception as exc:
            failures += 1
            record["status"] = "failed"
            record["error"] = f"{type(exc).__name__}: {exc}"
            print(f"[{index}/{len(documents)}] FAIL {record['error']}", file=sys.stderr, flush=True)
        finally:
            record["seconds"] = round(time.monotonic() - started, 2)
            write_manifest(manifest_path, manifest)
        print(
            f"[{index}/{len(documents)}] {record['status'].upper()} "
            f"pages={record['structured_pages']} seconds={record['seconds']}",
            flush=True,
        )

    manifest["completed_at"] = utc_now()
    manifest["success_count"] = len(documents) - failures
    manifest["failure_count"] = failures
    write_manifest(manifest_path, manifest)
    print(f"manifest={manifest_path} success={len(documents) - failures}/{len(documents)}", flush=True)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
