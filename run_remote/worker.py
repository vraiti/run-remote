#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "pydantic>=2",
# ]
# ///
"""run-remote-worker.py: runs on the remote host, invoked as
`uv run worker.py <job.json path>`. Ensures the job's venv exists,
garbage-collects orphaned ones, daemonizes (detaching from the ssh session
that launched it), then spawns the target command and records its exit
code.

The inline dependency block above lets `uv run` create an ephemeral,
cached environment with pydantic on first use -- the remote host's system
Python needs nothing pre-installed for this script itself (it already
needs `uv` regardless, for venv creation). This is unrelated to, and
entirely separate from, the job's own venv that venvspec.py builds.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# The shipped toolset's root, one level up from this package, where
# sync.sync_toolset puts commands/ next to run_remote/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import venvspec  # pylint: disable=wrong-import-position
from models import CommandSpec, JobSpec, VenvSpec  # pylint: disable=wrong-import-position

# Multi-command jobs: how long the other commands get between SIGTERM and
# SIGKILL once the first one exits.
STOP_GRACE_S = 30


def venv_spec_hash(spec_json: str) -> str:
    return hashlib.sha256(spec_json.encode()).hexdigest()


def ensure_venv(job: JobSpec, name: str, spec: VenvSpec) -> str:
    """Builds (or reuses, by spec hash) one venv and links it under the
    profile: ~/.venvs/<profile> for the top-level `venv` (the empty name),
    ~/.venvs/<profile>--<name> for a `venvs` entry."""
    spec_json = json.dumps(spec.model_dump(), separators=(",", ":"), sort_keys=True)
    digest = venv_spec_hash(spec_json)
    real_venv_dir = os.path.join(job.venvs_root, "venvs", f"venv-{digest}")
    link_name = job.profile_name if not name else f"{job.profile_name}--{name}"
    profile_venv_dir = os.path.join(job.venvs_root, link_name)

    if not os.path.isdir(real_venv_dir):
        print(f"venv {name or '(top-level)'} not found at {real_venv_dir}, creating from spec...")
        venvspec.build_venv_from_spec(spec, real_venv_dir, job.project_root)

    Path(profile_venv_dir).parent.mkdir(parents=True, exist_ok=True)
    tmp_link = f"{profile_venv_dir}.tmp-{os.getpid()}"
    os.symlink(real_venv_dir, tmp_link)
    os.replace(tmp_link, profile_venv_dir)  # atomic re-point, same as ln -sfn

    return real_venv_dir


def gc_orphaned_venvs(venvs_root: str) -> None:
    """Removes any ~/.venvs/venvs/venv-* not referenced by any symlink under
    ~/.venvs/ (recursively -- a profile name can contain "/"), skipping the
    venvs/ storage dir itself so a venv's own internal symlinks (e.g.
    bin/python -> python3) are never mistaken for profile references."""
    venvs_root_path = Path(venvs_root)
    storage_dir = venvs_root_path / "venvs"
    if not storage_dir.is_dir():
        return

    referenced: set[str] = set()
    for path in venvs_root_path.rglob("*"):
        if storage_dir in path.parents or path == storage_dir:
            continue
        if path.is_symlink():
            referenced.add(str(path.resolve()))

    for candidate in storage_dir.iterdir():
        if candidate.is_dir() and str(candidate.resolve()) not in referenced:
            print(f"Removing orphaned venv: {candidate}")
            shutil.rmtree(candidate)


def daemonize(log_file: str) -> None:
    """Double-fork + setsid so this process is fully detached from the ssh
    session's controlling terminal -- the parent (still attached to ssh)
    exits once the child has forked, so the ssh invocation that launched
    this returns deterministically rather than relying on shell job-control
    (`nohup`/`disown`) semantics.

    Flushes stdio and uses os._exit (not sys.exit) for both fork-away
    parents: fork() duplicates the process's buffered-but-unflushed stdio,
    and sys.exit() runs normal interpreter shutdown (which flushes that
    buffer) in each exiting parent -- without the explicit flush before
    forking and os._exit after, any output already printed (e.g. venv
    creation progress) would land in the buffer at fork time and then get
    flushed twice, once by each intermediate parent exiting.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)

    Path(os.path.dirname(log_file) or ".").mkdir(parents=True, exist_ok=True)
    devnull = os.open(os.devnull, os.O_RDONLY)
    log_fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.dup2(devnull, 0)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    if devnull > 2:
        os.close(devnull)
    if log_fd > 2:
        os.close(log_fd)


def ensure_system_packages(packages: list[str]) -> None:
    if not packages:
        return
    print(f"Ensuring system packages: {', '.join(packages)}")
    subprocess.run(["sudo", "dnf", "install", "-y", *packages], check=True)


def command_env(job: JobSpec, venv_dir: str) -> dict[str, str]:
    env = dict(os.environ)
    env["VIRTUAL_ENV"] = venv_dir
    env["PATH"] = f"{os.path.join(venv_dir, 'bin')}:{env.get('PATH', '')}"
    env.update(job.env)
    return env


def _pump_prefixed(stream, prefix: str, lock: threading.Lock) -> None:
    """Copies one command's merged stdout/stderr into the job log, line by
    line, each line tagged with the command it came from."""
    for raw in iter(stream.readline, b""):
        line = raw.decode(errors="replace")
        with lock:
            sys.stdout.write(f"{prefix} {line}" if line.endswith("\n") else f"{prefix} {line}\n")
            sys.stdout.flush()
    stream.close()


def _stop(proc: subprocess.Popen) -> None:
    """SIGTERM a command's whole process group, SIGKILL after the grace period."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    except ProcessLookupError:
        pass


def run_parallel(job: JobSpec, commands: list[CommandSpec], venv_dirs: dict[str, str]) -> int:
    """Launches every command at once, each in its own process group. The
    job ends when the first command exits: the others are stopped (a server
    paired with its client never exits on its own), and that first exit
    code is the job's."""
    lock = threading.Lock()
    procs: list[tuple[str, subprocess.Popen]] = []
    pumps: list[threading.Thread] = []
    try:
        for index, spec in enumerate(commands):
            label = f"[{index}:{spec.venv or 'venv'}]"
            print(f"{label} starting: {' '.join(spec.command)}", flush=True)
            proc = subprocess.Popen(  # pylint: disable=consider-using-with
                spec.command,
                cwd=job.project_root,
                env=command_env(job, venv_dirs[spec.venv or ""]),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            procs.append((label, proc))
            pump = threading.Thread(target=_pump_prefixed, args=(proc.stdout, label, lock), daemon=True)
            pump.start()
            pumps.append(pump)

        while True:
            finished = [(label, proc) for label, proc in procs if proc.poll() is not None]
            if finished:
                label, proc = finished[0]
                break
            time.sleep(0.5)
        print(f"{label} exited with {proc.returncode}; stopping the other commands", flush=True)
        return proc.returncode
    finally:
        for _label, other in procs:
            _stop(other)
        for pump in pumps:
            pump.join(timeout=5)


def run_job(job: JobSpec, venv_dirs: dict[str, str]) -> int:
    if job.initializer:
        # Kept as a real shell snippet on purpose -- documented as relying
        # on `&&`-chaining, unlike the main command below. Runs once, in the
        # first command's venv.
        env = command_env(job, venv_dirs[job.commands[0].venv or ""])
        subprocess.run(["bash", "-c", job.initializer], cwd=job.project_root, env=env, check=True)

    if len(job.commands) == 1:
        spec = job.commands[0]
        # Deliberately not check=True -- the whole point is to capture the
        # job's real exit code ourselves, success or failure, not raise on
        # nonzero.
        env = command_env(job, venv_dirs[spec.venv or ""])
        result = subprocess.run(spec.command, cwd=job.project_root, env=env, check=False)
        return result.returncode
    return run_parallel(job, job.commands, venv_dirs)


def apply_envvars(envvars: list[str]) -> None:
    for entry in envvars:
        if "=" not in entry:
            raise ValueError(f"envvars entry must be KEY=VALUE, got {entry!r}")
        key, _, value = entry.partition("=")
        os.environ[key] = value


def main() -> None:
    job_path = sys.argv[1]
    job = JobSpec.model_validate_json(Path(job_path).read_text(encoding="utf-8"))

    # Applied to this process's own environment before anything else below
    # is processed -- venv build, system packages, initializer, command all
    # inherit it for free from here on, the same way a real shell's exported
    # vars flow into every subprocess it spawns.
    apply_envvars(job.envvars)

    # Before the venv build, not just before the job command -- a
    # fini-command (e.g. one that needs nvcc from cuda-toolkit) runs as
    # part of ensure_venv below, so system packages have to already be
    # present by then, not merely by the time run_job's command starts.
    ensure_system_packages(job.system_packages)

    venv_dirs = {name: ensure_venv(job, name, spec) for name, spec in job.venvs.items()}
    gc_orphaned_venvs(job.venvs_root)

    daemonize(job.log_file)
    try:
        exit_code = run_job(job, venv_dirs)
    except Exception as e:  # pylint: disable=broad-exception-caught
        # Deliberately catches anything: this runs after daemonize(), so the
        # local watch loop is already blocked waiting on job.exit_file --
        # any unhandled exception here would leave it waiting forever
        # instead of getting a (failure) exit code back.
        print(f"ERROR: {e}", file=sys.stderr)
        exit_code = 1
    Path(job.exit_file).write_text(str(exit_code), encoding="utf-8")


if __name__ == "__main__":
    main()
