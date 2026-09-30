"""Syncs every repo named in a profile's `sync` map to <remote-root> on
<ssh-alias> via rsync, plus this toolset's own working tree to an auxiliary
remote tmpdir for worker.py to run from.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from contextlib import contextmanager
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterator, NamedTuple

from models import Profile

from commands import git as gitw
from commands import rsync as rsyncw
from commands import ssh as sshw

TOOLSET_REMOTE_DIR = "/tmp/run-remote-toolset"
# Where this package's own files land under the shipped toolset tree (see
# sync_toolset below) -- mirrors main.py's REMOTE_PACKAGE_DIR.
REMOTE_PACKAGE_DIR = "run_remote"


def toolset_local_dir() -> Path:
    # The run-remote repo root, one level up from this package.
    return Path(__file__).resolve().parents[1]


def _git_ls_not_ignored(repo_dir: Path) -> list[str]:
    """Every path git considers "not ignored" in repo_dir: tracked
    (--cached) or untracked-but-not-gitignored (--others
    --exclude-standard). Ships this toolset's whole working tree honoring
    .gitignore (recipes/secrets/, **/__pycache__, ...)
    automatically, without a hand-maintained file list -- and, since it
    isn't `git archive`, still includes uncommitted-but-not-ignored edits,
    not just HEAD, matching the point of rsyncing directly instead of
    requiring a commit first."""
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    )
    # --cached still lists files deleted from the working tree but not yet
    # staged as deleted (e.g. mid-move); there is nothing to ship for those,
    # and rsync --files-from fails on a missing path.
    return [path for path in result.stdout.decode().split("\0") if path and os.path.lexists(repo_dir / path)]


def sync_toolset(alias: str) -> str:
    """rsyncs this toolset's whole (non-gitignored) working tree to an
    auxiliary remote tmpdir, so worker.py exists there as a real file
    before it's ever invoked -- directly, not via a committed/pushed git
    checkout, so it works regardless of whether the local working tree is
    even committed."""
    local_dir = toolset_local_dir()
    rsyncw.sync(str(local_dir), alias, TOOLSET_REMOTE_DIR, files_from=_git_ls_not_ignored(local_dir))
    # commands is a separate package (installed in the local venv): ship it
    # to where worker.py looks for it, next to run_remote/.
    rsyncw.sync(
        str(Path(gitw.__file__).resolve().parent),
        alias,
        f"{TOOLSET_REMOTE_DIR}/commands",
        exclude=["__pycache__", "*.egg-info", ".git"],
    )
    return TOOLSET_REMOTE_DIR


def resolve_repo_dir(repo_name: str, project_dir: str) -> str:
    if repo_name.startswith("/"):
        return repo_name
    return os.path.join(project_dir, repo_name)


class SyncEntry(NamedTuple):
    name: str
    local_dir: str
    rsync: bool
    git_push: bool
    storage_uri: "str | None"


def _sync_entries(profile: Profile, project_dir: str) -> list[SyncEntry]:
    """Normalizes profile.sync's {name: SyncTarget} map into a flat list.
    local_dir is site_package_overlay-aware -- name still names the
    remote-side directory either way (see models.SyncTarget)."""
    entries = []
    for name, target in profile.sync.items():
        source = target.site_package_overlay if target.site_package_overlay is not None else name
        local_dir = resolve_repo_dir(source, project_dir)
        entries.append(SyncEntry(name, local_dir, target.rsync, target.git_push, target.remote_artifact))
    return entries


def check_dependency(
    project_dir: str, sync_dir: str, upstream_dir: str, hook: str, *, log: Callable[..., None]
) -> None:
    """A profile's `dependencies` key is {<sync-dir>: {<upstream-dir>:
    <rebuild-hook>}} -- before syncing anything, rebuild sync_dir if
    upstream_dir's HEAD has moved since the last rebuild. A per-upstream
    marker file (inside sync_dir) records the upstream commit the last
    rebuild ran against."""
    sync_path = os.path.join(project_dir, sync_dir)
    upstream_path = os.path.join(project_dir, upstream_dir)
    marker = os.path.join(sync_path, f".COMMIT_{upstream_dir}")

    if not os.path.exists(os.path.join(upstream_path, ".git")):
        print(f"WARNING: {upstream_path} is not a git repo, skipping dependency check for {sync_dir}")
        return
    upstream_head = gitw.rev_parse(upstream_path, "HEAD")

    if os.path.isfile(marker) and Path(marker).read_text(encoding="utf-8").strip() == upstream_head:
        return

    log(f"Rebuilding {sync_dir} ({upstream_dir} changed since last rebuild)...")
    subprocess.run(["bash", "-c", hook], cwd=project_dir, check=True)
    Path(sync_path).mkdir(parents=True, exist_ok=True)
    Path(marker).write_text(upstream_head + "\n", encoding="utf-8")


def push_repo_and_submodules(repo_dir: str) -> None:
    """For a `:push-only` repo, the remote is expected to `git pull` its own
    copy rather than receive one via rsync. Recurses into submodules so each
    gets pushed too. An auto-commit branch has no upstream yet -- resolve
    its remote from the branch it was based on, then establish tracking."""
    branch = gitw.current_branch(repo_dir)
    if branch is None:
        raise RuntimeError(f"cannot push {repo_dir} with detached HEAD")
    source_branch = branch.removeprefix("AUTOCOMMIT/")
    remote = gitw.config_get(repo_dir, f"branch.{source_branch}.remote")
    if not remote:
        raise RuntimeError(f"no remote configured for source branch {source_branch} in {repo_dir}")

    gitw.push(repo_dir, remote, branch)

    for _sha, path, initialized in gitw.submodule_status(repo_dir):
        if initialized:
            push_repo_and_submodules(os.path.join(repo_dir, path))


def _local_identity(repo_dir: str) -> str:
    """A git repo's identity is HEAD's commit (already committed and clean
    by the time this runs); a plain directory hashes every file's
    path/mtime/size instead, since there's no commit to key off of."""
    if os.path.exists(os.path.join(repo_dir, ".git")):
        return gitw.rev_parse(repo_dir, "HEAD")
    digest = hashlib.sha256()
    entries = []
    for root, _dirs, files in os.walk(repo_dir):
        for name in files:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, repo_dir)
            stat = os.stat(path)
            entries.append(f"{rel} {stat.st_mtime} {stat.st_size}")
    for line in sorted(entries):
        digest.update(line.encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _sync_one_repo(alias: str, remote_root: str, repo_name: str, repo_dir: str, *, log: Callable[..., None]) -> None:
    marker_path = f"{remote_root}/{repo_name}/.rrr-synced-commit"
    local_id = _local_identity(repo_dir)
    remote_id = sshw.read_remote_file(alias, marker_path, default="")
    if remote_id and remote_id.strip() == local_id:
        log(f"Skipping {repo_name} (unchanged since last sync)")
        return

    log(f"Syncing {repo_name}...")
    if os.path.exists(os.path.join(repo_dir, ".git")):
        # Export exactly the committed tree at HEAD, not the working
        # directory -- gitignore-filtering the working directory still lets
        # an untracked-but-not-ignored file through; the archive only ever
        # contains what's actually committed.
        with tempfile.TemporaryDirectory() as archive_dir:
            gitw.archive_repo_tree(repo_dir, local_id, archive_dir)
            rsyncw.sync(archive_dir, alias, f"{remote_root}/{repo_name}", exclude=[".rrr-synced-commit"])
    else:
        rsyncw.sync(repo_dir, alias, f"{remote_root}/{repo_name}", exclude=[".rrr-synced-commit"])

    sshw.run(alias, f"echo {sshw.quote(local_id)} > {sshw.quote(marker_path)}")


def check_sync_dirs_exist(project_dir: str, profile: Profile) -> None:
    """remote-artifact entries are exempt -- they're pulled fresh (or
    created empty) on the remote every run by prepare_artifacts, and never
    exist locally."""
    for entry in _sync_entries(profile, project_dir):
        if entry.storage_uri is None and not os.path.isdir(entry.local_dir):
            raise RuntimeError(f"{entry.local_dir} does not exist")


# pylint: disable-next=too-many-locals
def sync_all(alias: str, remote_root: str, project_dir: str, profile: Profile, *, quiet: bool = False) -> None:
    def log(*args: object) -> None:
        if not quiet:
            print(*args)

    entries = [e for e in _sync_entries(profile, project_dir) if e.storage_uri is None]

    check_sync_dirs_exist(project_dir, profile)

    # Auto-commit any uncommitted changes upfront, synchronously, for every
    # entry (including git-push-only ones) before any background push or
    # foreground rsync starts -- both touch the same repos (including
    # submodules), so committing lazily in either place would race two
    # concurrent commits against the same working tree/index.
    for entry in entries:
        if os.path.exists(os.path.join(entry.local_dir, ".git")):
            gitw.auto_commit_tree(entry.local_dir)

    for sync_dir, upstream_map in profile.dependencies.items():
        for upstream_dir, hook in upstream_map.items():
            check_dependency(project_dir, sync_dir, upstream_dir, hook, log=log)

    futures: list[tuple[Future, str]] = []
    with ThreadPoolExecutor() as executor:
        for entry in entries:
            # Silently a no-op if entry.local_dir isn't actually a git repo,
            # or its HEAD is detached (e.g. a site-package-overlay pinned to
            # a specific upstream commit) -- same as an unrequested push
            # would have been.
            has_head = os.path.exists(os.path.join(entry.local_dir, ".git")) and gitw.symbolic_ref_exists(
                entry.local_dir
            )
            if entry.git_push and has_head:
                futures.append((executor.submit(push_repo_and_submodules, entry.local_dir), entry.name))
            if entry.rsync:
                _sync_one_repo(alias, remote_root, entry.name, entry.local_dir, log=log)

        for future, repo_name in futures:
            try:
                future.result()
            except Exception as e:  # pylint: disable=broad-exception-caught
                # One repo's push failing shouldn't crash the whole sync or
                # hide the other repos' results -- report and move on, same
                # as a background job's own failure would just log.
                print(f"git push ({repo_name}) FAILED: {e}")


def _local_registry_auth() -> str | None:
    path = os.environ.get("REGISTRY_AUTH_FILE")
    return path if path and os.path.isfile(path) else None


REMOTE_REGISTRY_AUTH = "/tmp/run-remote-registry-auth.json"


@contextmanager
def _remote_registry_auth(alias: str) -> Iterator[str | None]:
    """Copies the operator's local $REGISTRY_AUTH_FILE (same convention
    skopeo/podman/buildah already use) to the remote for the duration of
    one oras call, and removes it again afterward."""
    local = _local_registry_auth()
    if local is None:
        yield None
        return
    sshw.run(
        alias,
        f"umask 077 && cat > {sshw.quote(REMOTE_REGISTRY_AUTH)}",
        input=Path(local).read_text(encoding="utf-8"),
    )
    try:
        yield REMOTE_REGISTRY_AUTH
    finally:
        sshw.run(alias, f"rm -f {sshw.quote(REMOTE_REGISTRY_AUTH)}", check=False)


def _run_remote_artifact(alias: str, remote_toolset_dir: str, args: list[str], registry_config: str | None) -> str:
    script = f"{remote_toolset_dir}/{REMOTE_PACKAGE_DIR}/artifact.py"
    argv = ["python3", script, *args]
    if registry_config is not None:
        argv += ["--registry-config", registry_config]
    result = sshw.run(alias, " ".join(sshw.quote(a) for a in argv))
    return result.stdout.strip().splitlines()[-1]


def prepare_artifacts(
    alias: str, remote_root: str, remote_toolset_dir: str, profile: Profile, *, quiet: bool = False
) -> dict[str, str]:
    """For every remote-artifact sync entry: on the remote, wipes its
    directory and pulls storageUri's tar into it (or leaves it empty if
    storageUri doesn't exist yet) -- nothing is pulled locally. Returns
    {entry name: baseline_content_hash} for sync_artifacts_back to diff
    against once the job has run."""

    def log(*args: object) -> None:
        if not quiet:
            print(*args)

    baseline: dict[str, str] = {}
    for name, target in profile.sync.items():
        if target.remote_artifact is None:
            continue
        remote_dir = f"{remote_root}/{name}"
        log(f"Pulling {target.remote_artifact} -> {alias}:{remote_dir}...")
        with _remote_registry_auth(alias) as auth:
            baseline[name] = _run_remote_artifact(
                alias, remote_toolset_dir, ["pull", target.remote_artifact, remote_dir], auth
            )
    return baseline


REMOTE_LOGS_DIR = "/tmp/logs"


def pull_logs(alias: str, local_root: str, *, quiet: bool = False) -> None:
    """Copies the remote's /tmp/logs into <local_root>/run-remote/tmp/logs,
    overwriting files that already exist there and keeping any the remote no
    longer has. A failed pull only warns: the job's own result stands."""
    local_dir = os.path.join(local_root, "run-remote", "tmp", "logs")
    os.makedirs(local_dir, exist_ok=True)
    if not quiet:
        print(f"Pulling {REMOTE_LOGS_DIR} into {local_dir}...")
    try:
        rsyncw.pull(alias, REMOTE_LOGS_DIR, local_dir, delete=False)
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"WARNING: failed to pull {REMOTE_LOGS_DIR} from {alias}: {e}")


def sync_artifacts_back(
    alias: str,
    remote_root: str,
    remote_toolset_dir: str,
    profile: Profile,
    baseline_hashes: dict[str, str],
    *,
    quiet: bool = False,
) -> None:
    """After the job has run, for each remote-artifact sync entry: on the
    remote, re-hashes its directory against the baseline prepare_artifacts
    recorded and, only if it differs, packs it into an OCI artifact and
    pushes it to storageUri straight from the remote."""

    def log(*args: object) -> None:
        if not quiet:
            print(*args)

    for name, target in profile.sync.items():
        if target.remote_artifact is None:
            continue
        if _local_registry_auth() is None:
            print(f"WARNING: no $REGISTRY_AUTH_FILE set, skipping change check/push of {name}")
            continue
        remote_dir = f"{remote_root}/{name}"
        try:
            with _remote_registry_auth(alias) as auth:
                status = _run_remote_artifact(
                    alias,
                    remote_toolset_dir,
                    ["push", target.remote_artifact, remote_dir, baseline_hashes.get(name, "")],
                    auth,
                )
        except Exception as e:  # pylint: disable=broad-exception-caught
            # One artifact's push failing shouldn't fail an otherwise-
            # successful job -- same posture as the background git pushes
            # in sync_all.
            print(f"WARNING: failed to push {name} to {target.remote_artifact}: {e}")
            continue
        if status == "pushed":
            log(f"{name} changed, pushed to {target.remote_artifact}")
        else:
            log(f"{name} unchanged, not pushing")
