#!/usr/bin/env python3
"""Local entrypoint/CLI: `run-remote [-q] <profile> [-- extra args...]`.
Resolves the host, syncs project repos plus this toolset itself, launches
the remote worker, streams its output back with reconnect support, and
exits with the job's real exit code.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# The repo root (one level up from this package): on the remote, the shipped
# toolset keeps commands/ there, next to run_remote/ (see sync.sync_toolset).
# Locally commands is installed in the venv.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hostresolve  # pylint: disable=wrong-import-position
import recipes  # pylint: disable=wrong-import-position
import sync  # pylint: disable=wrong-import-position
from models import JobSpec, Profile  # pylint: disable=wrong-import-position

from aws import poller as aws_poller  # pylint: disable=wrong-import-position
from commands import ssh as sshw  # pylint: disable=wrong-import-position

# Where this package's own files land under the shipped toolset tree
# (sync.sync_toolset ships the repo root, preserving this same relative
# path) -- worker.py and tail_watch.py are invoked from here, not from
# remote_toolset_dir's own top level.
REMOTE_PACKAGE_DIR = "run_remote"


def parse_args(argv: list[str]) -> tuple[bool, str, list[str]]:
    quiet = False
    rest: list[str] = []
    for arg in argv:
        if arg == "-q":
            quiet = True
        else:
            rest.append(arg)

    main_args: list[str] = []
    append_args: list[str] = []
    if "--" in rest:
        i = rest.index("--")
        main_args, append_args = rest[:i], rest[i + 1 :]
    else:
        main_args = rest

    if len(main_args) != 1:
        print(f"Usage: {sys.argv[0]} [-q] <profile> [-- args...]", file=sys.stderr)
        sys.exit(1)
    return quiet, main_args[0], append_args


def load_profile_secrets(profile_name: str) -> dict[str, str]:
    secrets_path = recipes.secrets_path(profile_name)
    if not secrets_path.is_file():
        return {}
    secrets: dict[str, str] = {}
    for line in secrets_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition("=")
        secrets[key] = value
    return secrets


def resolve_alias(profile: Profile) -> str:
    """host wins if set; otherwise a non-empty instanceFilters (see
    aws-refactor.md) delegates to the round-robin poller to find-or-create
    a matching instance; otherwise falls back to the single
    aws-manage-managed default host."""
    if profile.host:
        return profile.host
    if profile.instance_filters:
        return aws_poller.request_instance(profile.instance_filters)
    return hostresolve.resolve_default_host()


def watch_job(alias: str, remote_toolset_dir: str, log_file: str, exit_file: str, *, quiet: bool) -> int:
    offset = 0
    while True:
        cmd = (
            f"python3 {sshw.quote(f'{remote_toolset_dir}/{REMOTE_PACKAGE_DIR}/tail_watch.py')} "
            f"{sshw.quote(log_file)} {sshw.quote(exit_file)} --from-offset {offset}"
        )
        sshw.run_tty(alias, cmd)

        if sshw.test_path(alias, exit_file, kind="f"):
            break

        if not quiet:
            print(f"Connection to {alias} dropped, reconnecting in 5s...", file=sys.stderr)
        time.sleep(5)
        # Ask the remote for the log's true current size rather than trying
        # to count bytes as they streamed through the just-dropped ssh -tt
        # session -- a dropped connection gives tail_watch.py no chance to
        # report anything back itself, so the only reliable source of "how
        # much has actually been written" is a fresh, separate query.
        size_result = sshw.run(alias, f"stat -c%s {sshw.quote(log_file)} 2>/dev/null", check=False)
        if size_result.returncode == 0 and size_result.stdout.strip():
            offset = int(size_result.stdout.strip())

    exit_code_str = sshw.read_remote_file(alias, exit_file, default="1")
    sshw.run(alias, f"rm -f {sshw.quote(exit_file)}", check=False)
    return int((exit_code_str or "1").strip() or "1")


def main() -> int:  # pylint: disable=too-many-locals
    quiet, profile_name, append_args = parse_args(sys.argv[1:])

    raw_profile = recipes.load_profile(profile_name)
    try:
        profile = Profile.model_validate(raw_profile)
        venvs, commands = profile.run_units(append_args)
    except ValueError as e:  # pydantic's ValidationError is a ValueError
        print(f'ERROR: profile "{profile_name}": {e}', file=sys.stderr)
        return 1

    secrets = load_profile_secrets(profile_name)
    env = {**profile.env, **secrets}

    if not quiet:
        print(f"Using profile '{profile_name}':")
        print(recipes.yaml.dump(raw_profile, sort_keys=False, default_flow_style=False, allow_unicode=True), end="")
        if secrets:
            print("Using secrets:")
            for key in secrets:
                print(key)

    # Before resolve_alias, which for instanceFilters can mean acquiring or
    # creating a real EC2 instance.
    project_dir = profile.local_home or os.getcwd()
    sync.check_sync_dirs_exist(project_dir, profile)

    alias = resolve_alias(profile)

    # remote_root_template may contain a literal, unexpanded "$HOME" (the
    # default) -- resolve it against the remote's own $HOME via a plain,
    # unquoted `echo` before using it anywhere else, so every later use
    # below is an already-concrete path.
    remote_root_template = profile.home or "$HOME/vraiti"
    remote_root = sshw.run(alias, f"echo {remote_root_template}").stdout.strip()
    sshw.run(alias, f"mkdir -p {sshw.quote(remote_root)}")

    remote_home = sshw.echo_env(alias, "HOME")
    venvs_root = f"{remote_home}/.venvs"

    baseline_hashes = sync.prepare_artifacts(alias, remote_root, project_dir, profile, quiet=quiet)
    sync.sync_all(alias, remote_root, project_dir, profile, quiet=quiet)
    remote_toolset_dir = sync.sync_toolset(alias)

    # -- extra args (from the CLI) are appended after the profile's own
    # command, not a replacement for it -- e.g. `run-remote vllm-omni/pytest
    # -- -k test_foo` runs the profile's usual command with one extra arg.
    # (Profile.run_units applies them; multi-command profiles reject them.)

    log_file = f"/tmp/logs/rrr-{time.strftime('%Y%m%d-%H%M%S')}.log"
    exit_file = f"/tmp/.rrr_exit_{os.getpid()}_{int(time.time())}"

    job = JobSpec(
        profile_name=profile_name,
        venvs=venvs,
        envvars=profile.envvars,
        env=env,
        project_root=remote_root,
        initializer=profile.initializer,
        commands=commands,
        venvs_root=venvs_root,
        log_file=log_file,
        exit_file=exit_file,
        system_packages=profile.system.packages,
    )

    job_json_path = f"{remote_toolset_dir}/job.json"
    sshw.run(alias, f"cat > {sshw.quote(job_json_path)}", input=job.model_dump_json())
    # uv run, not a bare python3 -- worker.py declares its own dependencies
    # (pydantic) via inline PEP 723 metadata, so uv builds/caches an
    # ephemeral environment for it on first use instead of requiring the
    # remote's system Python to have pydantic pre-installed.
    worker_path = sshw.quote(f"{remote_toolset_dir}/{REMOTE_PACKAGE_DIR}/worker.py")
    sshw.run(alias, f"uv run {worker_path} {sshw.quote(job_json_path)}", capture=False)

    exit_code = watch_job(alias, remote_toolset_dir, log_file, exit_file, quiet=quiet)
    # Runs regardless of the job's own exit code -- an artifact the command
    # modified before later failing should still be pulled back and saved.
    sync.sync_artifacts_back(alias, remote_root, remote_toolset_dir, project_dir, profile, baseline_hashes, quiet=quiet)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
