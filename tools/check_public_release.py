#!/usr/bin/env python3
"""Reject document data and machine-local validation references before release."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import PurePosixPath


BLOCKED_SUFFIXES = {
    ".pdf",
    ".hwp",
    ".hwpx",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".odt",
    ".rtf",
    ".eml",
}
BLOCKED_PARTS = {
    ".verify",
    "documents",
    "processed",
    "uploads",
    "private_data",
    "validation_corpus",
}
BLOCKED_CONTENT = {
    "machine-local path": re.compile(rb"/(?:data\d*|home)/[^\s\"']+/"),
    "validation run id": re.compile(rb"\.verify/runs/\d+"),
}
MAX_TEXT_SCAN_BYTES = 4 * 1024 * 1024


def _git(*args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.check_output(["git", *args], input=input_bytes)


def _paths(ref: str | None, staged: bool) -> list[str]:
    if staged:
        raw = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    elif ref:
        raw = _git("ls-tree", "-r", "--name-only", "-z", ref)
    else:
        raw = _git("ls-files", "-z")
    return [item.decode("utf-8", "surrogateescape") for item in raw.split(b"\0") if item]


def _blob(path: str, ref: str | None, staged: bool) -> bytes:
    if staged:
        return _git("show", f":{path}")
    if ref:
        return _git("show", f"{ref}:{path}")
    with open(path, "rb") as handle:
        return handle.read(MAX_TEXT_SCAN_BYTES + 1)


def audit(
    ref: str | None = None,
    staged: bool = False,
    private_patterns: tuple[bytes, ...] = (),
) -> list[str]:
    failures = []
    for path in _paths(ref, staged):
        if not ref and not staged and not os.path.exists(path):
            continue
        pure = PurePosixPath(path)
        if pure.suffix.lower() in BLOCKED_SUFFIXES:
            failures.append(f"document file is tracked: {path}")
            continue
        if BLOCKED_PARTS.intersection(pure.parts):
            failures.append(f"private/runtime path is tracked: {path}")
            continue
        try:
            data = _blob(path, ref, staged)
        except (OSError, subprocess.CalledProcessError):
            failures.append(f"cannot inspect tracked file: {path}")
            continue
        if len(data) > MAX_TEXT_SCAN_BYTES or b"\0" in data:
            continue
        for label, pattern in BLOCKED_CONTENT.items():
            if pattern.search(data):
                failures.append(f"{label} in tracked text: {path}")
        if any(pattern in data for pattern in private_patterns):
            failures.append(f"private validation text in tracked file: {path}")
    return failures


def _private_patterns(path: str | None) -> tuple[bytes, ...]:
    if not path:
        return ()
    with open(path, "rb") as handle:
        return tuple(
            line.strip()
            for line in handle
            if line.strip() and not line.lstrip().startswith(b"#")
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--ref", help="audit the tree at a Git revision")
    group.add_argument(
        "--history-ref", help="audit every commit reachable from a Git revision"
    )
    group.add_argument("--staged", action="store_true", help="audit the Git index")
    parser.add_argument(
        "--patterns-file",
        help="optional private file containing one blocked literal per line",
    )
    args = parser.parse_args()
    patterns = _private_patterns(args.patterns_file)
    if args.history_ref:
        commits = _git("rev-list", args.history_ref).decode().splitlines()
        failures = sorted(
            {
                failure
                for commit in commits
                for failure in audit(ref=commit, private_patterns=patterns)
            }
        )
    else:
        failures = audit(
            ref=args.ref,
            staged=args.staged,
            private_patterns=patterns,
        )
    if failures:
        print("Public release audit failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("Public release audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
