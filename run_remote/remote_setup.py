"""Brings a remote up to the base state jobs expect, beyond what the AMI
provides (the NVIDIA driver and CUDA toolkit; see create-from-rhel10-ami.sh).

Runs from the local side over ssh, before the worker: the worker itself runs
under uv, so uv has to exist first. Each ensure_<package> checks for its
package and installs it only when missing, so a warm instance pays one quick
check per package.
"""
from __future__ import annotations

from commands import ssh as sshw


def _ensure(alias: str, name: str, check: str, install: str, *, quiet: bool) -> None:
    if sshw.run(alias, check, check=False).returncode == 0:
        return
    if not quiet:
        print(f"Installing {name} on {alias}...")
    sshw.run(alias, install, capture=quiet)


def _ensure_rpm(alias: str, package: str, *, quiet: bool) -> None:
    _ensure(
        alias,
        package,
        f"rpm -q {sshw.quote(package)} >/dev/null 2>&1",
        f"sudo dnf install -y {sshw.quote(package)}",
        quiet=quiet,
    )


def ensure_uv(alias: str, *, quiet: bool = False) -> None:
    """Runs the worker (`uv run worker.py`) and builds the job venvs.
    Installs into ~/.local/bin, which is on the non-interactive ssh PATH."""
    _ensure(
        alias,
        "uv",
        "command -v uv >/dev/null 2>&1",
        "curl -LsSf https://astral.sh/uv/install.sh | sh",
        quiet=quiet,
    )


def ensure_python3_pip(alias: str, *, quiet: bool = False) -> None:
    _ensure_rpm(alias, "python3-pip", quiet=quiet)


def ensure_python3_devel(alias: str, *, quiet: bool = False) -> None:
    _ensure_rpm(alias, "python3-devel", quiet=quiet)


def ensure_git(alias: str, *, quiet: bool = False) -> None:
    _ensure_rpm(alias, "git", quiet=quiet)


def ensure_mesa_libgl(alias: str, *, quiet: bool = False) -> None:
    """libGL.so.1, an import-time dependency of opencv-python (pulled in by
    vllm-omni for its multimodal/video pipeline) that RHEL 10 minimal lacks."""
    _ensure_rpm(alias, "mesa-libGL", quiet=quiet)


def ensure_sqlite_devel(alias: str, *, quiet: bool = False) -> None:
    """sqlite3.h, needed to build CPython (e.g. python-tracer's cpython
    submodule) with sqlite support."""
    _ensure_rpm(alias, "sqlite-devel", quiet=quiet)


def ensure_zstd(alias: str, *, quiet: bool = False) -> None:
    """For tar --zstd, which remote-artifact entries' oras pull/push use."""
    _ensure_rpm(alias, "zstd", quiet=quiet)


def ensure_oras(alias: str, *, quiet: bool = False) -> None:
    """The latest oras release, into /usr/local/bin."""
    install = r"""set -e
version=$(curl -s https://api.github.com/repos/oras-project/oras/releases/latest | grep -Po '"tag_name": "v\K[^"]*')
tmp=$(mktemp -d)
curl -LsSf -o "$tmp/oras.tar.gz" "https://github.com/oras-project/oras/releases/download/v${version}/oras_${version}_linux_amd64.tar.gz"
tar -zxf "$tmp/oras.tar.gz" -C "$tmp"
sudo install -m 755 "$tmp/oras" /usr/local/bin/oras
rm -rf "$tmp"
"""
    _ensure(alias, "oras", "command -v oras >/dev/null 2>&1", f"bash -c {sshw.quote(install)}", quiet=quiet)


def ensure_base_packages(alias: str, *, quiet: bool = False) -> None:
    ensure_uv(alias, quiet=quiet)
    ensure_python3_pip(alias, quiet=quiet)
    ensure_python3_devel(alias, quiet=quiet)
    ensure_git(alias, quiet=quiet)
    ensure_mesa_libgl(alias, quiet=quiet)
    ensure_sqlite_devel(alias, quiet=quiet)
    ensure_zstd(alias, quiet=quiet)
    ensure_oras(alias, quiet=quiet)
