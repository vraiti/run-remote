#!/usr/bin/env python3
"""Runs on the remote via `ssh -tt`, invoked as
`tail_watch.py <log_file> <exit_file> --from-offset N`. Streams new bytes
from log_file (starting at byte offset N) to its own stdout until exit_file
appears, then exits. The caller accumulates how many bytes it has already
displayed across reconnects and passes the updated offset into each new
invocation, so a dropped connection resumes from where it left off instead
of replaying the whole log.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("log_file")
    parser.add_argument("exit_file")
    parser.add_argument("--from-offset", type=int, default=0)
    args = parser.parse_args()

    log_path = Path(args.log_file)
    exit_path = Path(args.exit_file)

    while not log_path.exists():
        if exit_path.exists():
            return
        time.sleep(0.2)

    with log_path.open("rb") as f:
        f.seek(args.from_offset)
        while True:
            chunk = f.read()
            if chunk:
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            elif exit_path.exists():
                # One more read in case the job wrote its final bytes and
                # the exit file in that order but this loop observed the
                # exit file first.
                chunk = f.read()
                if chunk:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                return
            else:
                time.sleep(0.2)


if __name__ == "__main__":
    main()
