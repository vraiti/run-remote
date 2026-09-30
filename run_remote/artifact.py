#!/usr/bin/env python3
"""Runs on the remote under its bare system python3 (stdlib plus the
shipped commands package) -- pulls a remote-artifact sync entry's
storageUri straight into its remote directory before the job, and pushes
it back after only if its content hash changed, so the artifact never
touches the local machine.

  artifact.py pull <ref> <dir> [--registry-config F]  -> prints baseline hash
  artifact.py push <ref> <dir> <baseline> [--registry-config F]
      -> prints "unchanged" or "pushed"
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from commands import oras  # pylint: disable=wrong-import-position
from content_hash import content_hash  # pylint: disable=wrong-import-position

EXCLUDE = {".rrr-synced-commit"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["pull", "push"])
    parser.add_argument("ref")
    parser.add_argument("directory")
    parser.add_argument("baseline", nargs="?")
    parser.add_argument("--registry-config")
    args = parser.parse_args()

    if args.action == "pull":
        shutil.rmtree(args.directory, ignore_errors=True)
        oras.pull(args.ref, args.directory, registry_config=args.registry_config)
        print(content_hash(args.directory, exclude=EXCLUDE))
        return

    after = content_hash(args.directory, exclude=EXCLUDE)
    if after == args.baseline:
        print("unchanged")
        return
    oras.push(args.directory, args.ref, after, registry_config=args.registry_config)
    print("pushed")


if __name__ == "__main__":
    main()
