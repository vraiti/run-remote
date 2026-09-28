"""Builds a venv from a profile's `venv` spec (see models.VenvSpec):
packages then requirements are installed in the given order (installs can
be order-dependent), then fini-commands run, each once, in order, inside
the finished venv. Called in-process by worker.py on the remote.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from models import VenvSpec

from commands import uv

DEFAULT_PYTHON = uv.DEFAULT_PYTHON


def _build_env(venv_dir: str) -> dict[str, str]:
    """Environment for package/requirements/fini-command subprocess calls.

    Equivalent to what the original bash version got for free from `source
    bin/activate` -- VIRTUAL_ENV set, the venv's bin/ prepended to PATH --
    plus the CUDA PATH fix. A fini-command can assume an activated venv
    (e.g. a bare `uv pip show flashinfer-python`, with no --python flag,
    relying on VIRTUAL_ENV to target the right one); without this, such a
    bare call resolves against whatever venv uv finds ambiently instead, not
    the one being built (confirmed: "Package(s) not found for:
    flashinfer-python" even though it was just installed, moments earlier,
    as one of vllm's own dependencies). job.envvars are already applied to
    this process's own os.environ (see worker.main) before this ever runs,
    so dict(os.environ) below already carries them.
    """
    env = dict(os.environ)
    cuda_bin = Path("/usr/local/cuda/bin")
    if cuda_bin.is_dir():
        env["PATH"] = f"{cuda_bin}:{env.get('PATH', '')}"
        cuda_lib = "/usr/local/cuda/lib64"
        env["LD_LIBRARY_PATH"] = f"{cuda_lib}:{env['LD_LIBRARY_PATH']}" if env.get("LD_LIBRARY_PATH") else cuda_lib
    env["VIRTUAL_ENV"] = venv_dir
    env["PATH"] = f"{os.path.join(venv_dir, 'bin')}:{env.get('PATH', '')}"
    return env


def build_venv_from_spec(spec: VenvSpec, venv_dir: str, project_root: str) -> None:
    python_version = spec.python or DEFAULT_PYTHON

    print(f"Creating venv at {venv_dir} (python {python_version})...")
    venv_path = Path(venv_dir)
    if venv_path.exists():
        shutil.rmtree(venv_path)
    venv_path.parent.mkdir(parents=True, exist_ok=True)
    uv.create_venv(venv_dir, python_version)
    env = _build_env(venv_dir)

    for package in spec.packages:
        # A package entry can be an argv-style string with flags (e.g. "-e
        # vllm-omni --no-build-isolation"); shlex.split (not a bare
        # str.split) matches shell word-splitting, including quoted tokens.
        print(f"Installing package: {package}")
        uv.pip_install(venv_dir, shlex.split(package), cwd=project_root, env=env)

    for requirements_file in spec.requirements:
        print(f"Installing requirements from {requirements_file}")
        uv.pip_install_requirements(
            venv_dir, os.path.join(project_root, requirements_file), cwd=project_root, env=env
        )

    for command in spec.fini_commands:
        print(f"Running fini-command: {command}")
        subprocess.run(["bash", "-c", command], cwd=project_root, env=env, check=True)

    print("Done.")
