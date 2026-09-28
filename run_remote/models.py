"""Pydantic schema for the two places this toolchain crosses a
serialization boundary: a profile's YAML on load, and the job.json handed
from run_remote.py (local) to worker.py (remote). Not a general dataclass
replacement -- commands/*.py's plain typed functions stay as they are.

worker.py imports this module on the remote host, outside of any job venv --
the remote's system python3 needs `pydantic` installed for that import to
succeed, the same way it already needs `pyyaml` for recipes.py's own YAML
shim.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class VenvSpec(BaseModel):
    """A profile's `venv` key: packages/requirements are installed in that
    order (installs can be order-dependent), then fini-commands run, each
    once, in order, inside the finished venv."""

    python: str | None = None
    packages: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    fini_commands: list[str] = Field(default_factory=list, alias="finiCommands")

    model_config = {"populate_by_name": True}


class CommandSpec(BaseModel):
    """One entry of a profile's `commands` list: an argv vector and the key
    (into the profile's `venvs` map) of the venv it runs in. Without a
    `venv` key it runs in the profile's top-level `venv`."""

    command: list[str]
    venv: str | None = None


class SystemSpec(BaseModel):
    """A profile's `system` key: OS-level setup applied before the
    initializer/command run, on top of whatever the base AMI already has."""

    packages: list[str] = Field(default_factory=list)


class SyncTarget(BaseModel):
    """A profile's `sync.<path>` value: a flag list, one of
    "rsync"/"git-push" (bare) or "site-package-overlay:<path>"/
    "remote-artifact:<storageUri>" (each carrying a colon-separated
    parameter -- storageUri itself may contain further colons, e.g. an OCI
    tag reference, so only the first colon is a delimiter).

    rsync syncs <path> (or site_package_overlay, if set) up to the remote
    normally; git_push additionally/instead pushes <path>'s own git remote
    in the background (silently a no-op if <path> isn't actually a git repo
    or its HEAD is detached); remote_artifact makes <path> an OCI-artifact-
    backed directory instead (pulled from storageUri before the job runs,
    pushed back only if changed, never trusted to persist locally) -- see
    sync.py's prepare_artifacts/sync_artifacts_back."""

    rsync: bool = False
    git_push: bool = False
    site_package_overlay: "str | None" = None
    remote_artifact: "str | None" = None

    @model_validator(mode="before")
    @classmethod
    def _from_flag_list(cls, data: object) -> object:
        if not isinstance(data, list):
            return data
        parsed: dict[str, Any] = {}
        for flag in data:
            if flag == "rsync":
                parsed["rsync"] = True
            elif flag == "git-push":
                parsed["git_push"] = True
            elif flag.startswith("site-package-overlay:"):
                parsed["site_package_overlay"] = flag.partition(":")[2]
            elif flag.startswith("remote-artifact:"):
                parsed["remote_artifact"] = flag.partition(":")[2]
            else:
                raise ValueError(f"unrecognized sync flag {flag!r}")
        return parsed

    @model_validator(mode="after")
    def _check_constraints(self) -> "SyncTarget":
        if self.remote_artifact is not None and (
            self.rsync or self.git_push or self.site_package_overlay is not None
        ):
            raise ValueError("remote-artifact cannot be combined with rsync/git-push/site-package-overlay")
        if self.site_package_overlay is not None and not self.rsync:
            raise ValueError("site-package-overlay requires rsync")
        if not (self.rsync or self.git_push or self.remote_artifact is not None):
            raise ValueError("sync entry has none of rsync/git-push/remote-artifact -- it would do nothing")
        return self


class InstanceFilter(BaseModel):
    # Deliberately not in run_remote/aws/ (which imports boto3) -- worker.py
    # runs on the remote, which has no boto3, and loads this file too.
    max_hourly_cost: float | None = Field(default=None, alias="maxHourlyCost")
    regions: list[str] | None = None
    instance_types: list[str] | None = Field(default=None, alias="instanceTypes")

    model_config = {"populate_by_name": True}


class Profile(BaseModel):
    """The full profile schema. Validated once, immediately after
    `recipes.load_profile()` -- catching a malformed profile here means the
    error surfaces before sync/ssh ever starts, instead of after a remote
    round-trip."""

    # A profile runs either one `command` or a `commands` list (launched in
    # parallel on the same host; they talk over loopback). Each runs in the
    # venv it names from `venvs`, or else in the top-level `venv` -- there is
    # no default venv, so one of the two must apply (see _check_run_shape).
    venv: VenvSpec | None = None
    venvs: dict[str, VenvSpec] = Field(default_factory=dict)
    commands: list[CommandSpec] = Field(default_factory=list)
    # Plain "KEY=VALUE" strings, applied (via os.environ) before worker.py
    # processes anything else -- system packages, the venv build, the
    # initializer, the command -- unlike `env`/`secret` below, which are
    # only merged into the job command's own environment at run time.
    envvars: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    host: str | None = None
    home: str | None = None
    local_home: str | None = Field(default=None, alias="localHome")
    initializer: str | None = None
    command: list[str] = Field(default_factory=list)
    sync: dict[str, SyncTarget] = Field(default_factory=dict)
    dependencies: dict[str, dict[str, str]] = Field(default_factory=dict)
    system: SystemSpec = Field(default_factory=SystemSpec)
    instance_filters: list[InstanceFilter | list[InstanceFilter]] = Field(
        default_factory=list, alias="instanceFilters"
    )

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _check_run_shape(self) -> "Profile":
        if self.command and self.commands:
            raise ValueError('a profile has either "command" or "commands", not both')
        if not self.command and not self.commands:
            raise ValueError('a profile needs "command" or "commands"')
        for name in self.venvs:
            if not name or "/" in name:
                raise ValueError(f"venvs key {name!r} must be a non-empty name without '/'")
        for label, entry in self._entries():
            if not entry.command:
                raise ValueError(f"{label} is empty")
            if entry.venv is None:
                if self.venv is None:
                    raise ValueError(f'{label} names no venv and the profile has no top-level "venv"')
            elif entry.venv not in self.venvs:
                raise ValueError(f"{label} names venv {entry.venv!r}, which is not a key of venvs")
        return self

    def _entries(self) -> list[tuple[str, CommandSpec]]:
        if self.commands:
            return [(f"commands[{index}]", entry) for index, entry in enumerate(self.commands)]
        return [('"command"', CommandSpec(command=self.command))]

    def run_units(self, append_args: list[str]) -> tuple[dict[str, VenvSpec], list[CommandSpec]]:
        """(venvs, commands) with every command's venv resolved to a key of
        the returned map -- the top-level `venv` under the empty key, which
        worker.py links at ~/.venvs/<profile>. Only venvs some command uses
        are returned (and built). CLI extra args extend the single `command`;
        a `commands` list has no single command to extend, so it rejects them."""
        if self.commands and append_args:
            raise ValueError('extra CLI args are only supported for profiles with a single "command"')
        venvs: dict[str, VenvSpec] = {}
        commands: list[CommandSpec] = []
        for _label, entry in self._entries():
            if entry.venv is None:
                assert self.venv is not None
                key, spec = "", self.venv
            else:
                key, spec = entry.venv, self.venvs[entry.venv]
            venvs[key] = spec
            argv = [*entry.command, *append_args] if not self.commands else list(entry.command)
            commands.append(CommandSpec(command=argv, venv=key))
        return venvs, commands


class JobSpec(BaseModel):
    """What run_remote.py sends worker.py as job.json -- the subset of a
    Profile actually needed once a job is ready to launch: env already
    merged with secrets, concrete (already `$HOME`-expanded) remote paths."""

    profile_name: str
    # Normalized by Profile.run_units: every command's venv is a key of
    # venvs; the empty key is the profile's top-level venv.
    venvs: dict[str, VenvSpec]
    envvars: list[str]
    env: dict[str, str]
    project_root: str
    initializer: str | None
    commands: list[CommandSpec]
    venvs_root: str  # "$HOME/.venvs" on the remote, already expanded
    log_file: str
    exit_file: str
    system_packages: list[str]
