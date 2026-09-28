#!/usr/bin/env python3
"""Prints the sha256 content-hash of a directory: for every file, sorted by
relative path, hashes the path then the file's bytes. Stdlib-only (no
pydantic) so it can run under the remote's bare system python3, unlike the
rest of this toolset -- used by sync.py to check, over one cheap ssh round
trip, whether an "artifact" sync entry's remote copy actually changed
before paying for a full rsync pull-back.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def content_hash(dir_path: str, *, exclude: set[str] | None = None) -> str:
    exclude = exclude or set()
    digest = hashlib.sha256()
    root = Path(dir_path)
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.name not in exclude:
                digest.update(path.relative_to(root).as_posix().encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory")
    parser.add_argument("--exclude", action="append", default=[], help="filename to skip (repeatable)")
    args = parser.parse_args()
    print(content_hash(args.directory, exclude=set(args.exclude)))


if __name__ == "__main__":
    main()
