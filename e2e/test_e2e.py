"""End-to-end test: three podman-compose services -- `local`, `git-server`,
`dev` -- on a shared compose network. `local` gets this repo's current
working tree at ~/projects/run-remote (pushed to git-server:run-remote) and
the commands package it depends on at ~/projects/commands, both installed
editable so the run-remote console script is on PATH, and a fresh
~/test-repo (pushed to git-server:test-repo) with an untracked test.py.
The test runs the .ci-tests/recipe profile from `local`, which sends the
job to `dev` over ssh, and checks the output is exactly "hello".

Container/network lifecycle is podman compose's job (see compose.yaml);
this file only does the imperative setup compose can't express statically
(runtime-generated ssh keys, git pushes, dropping in test.py) and the
actual test assertion.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterator

import commands
import pytest

E2E_DIR = Path(__file__).resolve().parent
RUN_REMOTE_DIR = E2E_DIR.parent
# The commands package run-remote imports, wherever it's installed locally.
COMMANDS_DIR = Path(commands.__file__).resolve().parent
COMPOSE_FILE = E2E_DIR / "compose.yaml"


def compose(*args: str) -> None:
    subprocess.run(["podman", "compose", "-f", str(COMPOSE_FILE), *args], check=True, cwd=E2E_DIR)


def podman_exec(
    container: str,
    *args: str,
    input: str | None = None,  # pylint: disable=redefined-builtin
    workdir: str | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    argv = ["podman", "exec"]
    if input is not None:
        argv.append("-i")
    if workdir is not None:
        argv += ["-w", workdir]
    for key, value in (env or {}).items():
        argv += ["-e", f"{key}={value}"]
    argv += [container, *args]
    return subprocess.run(
        argv, input=input, text=True, check=check, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )


def write_file(container: str, path: str, content: str) -> None:
    """Writes content to path inside container via stdin -- avoids the
    shell-quoting a heredoc-in-a-podman-exec-argument would otherwise need."""
    podman_exec(container, "bash", "-c", f"cat > {path}", input=content)


@pytest.fixture(scope="module")
def provisioned_stack() -> Iterator[None]:
    subprocess.run(["podman", "compose", "-f", str(COMPOSE_FILE), "down", "-v"], cwd=E2E_DIR, check=False)
    compose("up", "-d", "--build")
    try:
        for container in ("local", "git-server", "dev"):
            podman_exec(container, "/usr/sbin/sshd")

        podman_exec("local", "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", "/root/.ssh/id_ed25519")
        pubkey = podman_exec("local", "cat", "/root/.ssh/id_ed25519.pub").stdout

        for container in ("git-server", "dev"):
            podman_exec(container, "mkdir", "-p", "/root/.ssh")
            podman_exec(container, "bash", "-c", "cat >> /root/.ssh/authorized_keys", input=pubkey)
            podman_exec(container, "chmod", "700", "/root/.ssh")
            podman_exec(container, "chmod", "600", "/root/.ssh/authorized_keys")

        # Fresh host keys every run (podman compose down -v above wipes any
        # prior state) -- nothing to verify against, so skip it entirely.
        write_file(
            "local",
            "/root/.ssh/config",
            "Host *\n    StrictHostKeyChecking no\n    UserKnownHostsFile /dev/null\n    LogLevel ERROR\n",
        )
        podman_exec("local", "mkdir", "-p", "/root/.ssh/config.d")
        # This is the same file hostresolve.py reads for its default-host
        # resolution -- "dev" here is also the real setup's actual alias.
        write_file(
            "local",
            "/root/.ssh/config.d/awsm",
            "Host dev\n    HostName dev\n    User root\n    IdentityFile /root/.ssh/id_ed25519\n",
        )
        podman_exec("local", "chmod", "600", "/root/.ssh/id_ed25519")

        podman_exec("git-server", "git", "init", "-q", "--bare", "/root/git/run-remote.git")
        podman_exec("git-server", "git", "init", "-q", "--bare", "/root/git/test-repo.git")

        podman_exec("local", "mkdir", "-p", "/root/projects")
        subprocess.run(["podman", "cp", str(RUN_REMOTE_DIR), "local:/root/projects/run-remote"], check=True)
        subprocess.run(["podman", "cp", str(COMMANDS_DIR), "local:/root/projects/commands"], check=True)
        podman_exec(
            "local",
            "pip3",
            "install",
            "-q",
            "--break-system-packages",
            "-e",
            "/root/projects/commands",
            "-e",
            "/root/projects/run-remote",
        )
        podman_exec("local", "git", "config", "--global", "user.email", "e2e@example.com")
        podman_exec("local", "git", "config", "--global", "user.name", "E2E Test")
        podman_exec("local", "git", "config", "--global", "--add", "safe.directory", "/root/projects/run-remote")
        podman_exec(
            "local",
            "bash",
            "-c",
            "cd /root/projects/run-remote && git add -A && git commit -q -m 'e2e snapshot' --allow-empty --no-verify",
        )
        podman_exec(
            "local",
            "bash",
            "-c",
            "cd /root/projects/run-remote && git remote remove origin 2>/dev/null; "
            "git remote add origin root@git-server:git/run-remote.git",
        )
        podman_exec("local", "bash", "-c", "cd /root/projects/run-remote && git push -q -u origin HEAD:refs/heads/main")

        podman_exec("local", "mkdir", "-p", "/root/test-repo")
        podman_exec("local", "bash", "-c", "cd /root/test-repo && git init -q")
        podman_exec(
            "local",
            "bash",
            "-c",
            "cd /root/test-repo && echo 'test-repo' > README.md && git add -A && git commit -q -m initial",
        )
        podman_exec(
            "local",
            "bash",
            "-c",
            "cd /root/test-repo && git remote add origin root@git-server:git/test-repo.git "
            "&& git push -q -u origin HEAD:refs/heads/main",
        )
        write_file("local", "/root/test-repo/test.py", 'print("hello")\n')

        yield
    finally:
        compose("down", "-v")


def test_run_remote_sends_job_to_dev_and_prints_hello(provisioned_stack: None) -> None:  # pylint: disable=unused-argument
    result = podman_exec(
        "local",
        "run-remote",
        "-q",
        ".ci-tests/recipe",
        workdir="/root",
        env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        check=False,
    )
    lines = [line.strip("\r") for line in result.stdout.splitlines() if line.strip()]
    assert result.returncode == 0, f"run-remote exited {result.returncode}:\n{result.stdout}"
    assert lines and lines[-1] == "hello", f"unexpected run-remote output:\n{result.stdout}"
    # The remote's /tmp/logs (holding the worker's rrr-*.log) is pulled into
    # $PWD/run-remote/tmp/logs when the job ends.
    pulled = podman_exec("local", "bash", "-c", "ls /root/run-remote/tmp/logs/rrr-*.log", check=False)
    assert pulled.returncode == 0, f"job log was not pulled back:\n{pulled.stdout}"
