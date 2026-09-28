"""Resolves the sole SSH alias managed by aws-manage (a `Host` block in
$SSH_CONFIG_FILE, i.e. ~/.ssh/config.d/awsm) so callers that take an
optional alias/host can fall back to "the one instance that exists" instead
of requiring it spelled out every time. Deliberately does NOT look at other
files under ~/.ssh/config.d/ -- only aws-manage's own aliases count as a
default host.

Plain text parsing, no subprocess -- this was never really "external," just
glue reading a config file.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

HOST_LINE_RE = re.compile(r"^Host\s+(\S+)", re.MULTILINE)


class NoDefaultHostError(RuntimeError):
    pass


class AmbiguousDefaultHostError(RuntimeError):
    def __init__(self, hosts: list[str], config_file: Path):
        self.hosts = hosts
        self.config_file = config_file
        listing = "\n".join(f"  {h}" for h in hosts)
        super().__init__(
            f"multiple aws-manage SSH aliases exist in {config_file}, host must be "
            f"specified explicitly:\n{listing}"
        )


def _ssh_config_file() -> Path:
    # Overridable via the same SSH_CONFIG_FILE env var aws-manage itself
    # uses, so a caller can still point this at a non-default location.
    return Path(os.environ.get("SSH_CONFIG_FILE", Path.home() / ".ssh" / "config.d" / "awsm"))


def resolve_default_host(config_file: Path | None = None) -> str:
    config_file = config_file or _ssh_config_file()
    try:
        text = config_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""

    hosts = sorted(set(HOST_LINE_RE.findall(text)))

    if not hosts:
        raise NoDefaultHostError(f"no aws-manage SSH alias found in {config_file}")
    if len(hosts) > 1:
        raise AmbiguousDefaultHostError(hosts, config_file)
    return hosts[0]


if __name__ == "__main__":
    import sys

    try:
        print(resolve_default_host())
    except (NoDefaultHostError, AmbiguousDefaultHostError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
