#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
"""Manages run-remote profiles: YAML files under recipes/recipes/,
selectable via `run-remote <profile>`. Profiles are hand-edited YAML --
`mod` is the only way to create or change one, and just opens $EDITOR on
the file (creating an empty stub first if it doesn't exist yet)."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import argcomplete
import yaml

# __file__-relative (not Path.home()-anchored) -- recipes.py lives at
# run_remote/recipes.py, and the actual recipe/secret
# files live two levels up, under the sibling run-remote/recipes/ directory.
_RECIPES_ROOT = Path(__file__).resolve().parent.parent / "recipes"
PROFILE_DIR = _RECIPES_ROOT / "recipes"
SECRETS_DIR = _RECIPES_ROOT / "secrets"


def list_profile_names() -> list[str]:
    if not PROFILE_DIR.is_dir():
        return []
    # A profile name may contain "/" (mod/mv make the subdirectories as
    # needed), so this has to recurse and keep the subdirectory prefix, not
    # just the bare filename stem.
    return sorted(
        str(p.relative_to(PROFILE_DIR).with_suffix(""))
        for p in PROFILE_DIR.rglob("*.yaml")
    )


def profile_name_completer(prefix: str, **_kwargs: Any) -> list[str]:
    return [name for name in list_profile_names() if name.startswith(prefix)]


def _set_completer(action: argparse.Action, completer: Callable[..., list[str]]) -> None:
    # argcomplete's documented usage pattern is exactly this attribute
    # assignment -- it monkey-patches argparse.Action at runtime, so
    # argparse's own stubs have no way to know about it statically.
    action.completer = completer  # type: ignore[attr-defined]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="recipes")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    mod = subparsers.add_parser("mod", help="Create or edit a profile in $EDITOR")
    _set_completer(mod.add_argument("profile_name"), profile_name_completer)

    subparsers.add_parser("ls", help="List profile names")

    show = subparsers.add_parser("get", help="Print a profile's resolved YAML")
    _set_completer(show.add_argument("profile_name"), profile_name_completer)

    delete = subparsers.add_parser("rm", help="Delete a profile")
    _set_completer(delete.add_argument("profile_name"), profile_name_completer)

    move = subparsers.add_parser(
        "mv",
        help="Rename/move a profile (and its secrets, if any); either name may contain "
             "'/' to nest it in a subdirectory",
    )
    _set_completer(move.add_argument("old_name"), profile_name_completer)
    move.add_argument("new_name")

    argcomplete.autocomplete(parser)
    return parser.parse_args(argv)


def profile_path(name: str) -> Path:
    return PROFILE_DIR / f"{name}.yaml"


def secrets_path(name: str) -> Path:
    return SECRETS_DIR / f"{name}.txt"


def _load_profile_yaml(name: str) -> dict[str, Any]:
    path = profile_path(name)
    if not path.is_file():
        print(f"ERROR: profile '{name}' not found at {path}", file=sys.stderr)
        sys.exit(1)
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _merge_leaf(existing: Any, incoming: Any) -> Any:
    """existing (the more-specific recipe's own value) always wins; incoming
    (an included recipe's value) only fills in what's missing. Maps merge
    key-by-key, recursively; lists get incoming's items prepended (so an
    include's list entries -- e.g. venv packages, install order matters --
    come before the recipe's own); anything else, or a type mismatch,
    keeps existing as-is."""
    if isinstance(existing, dict) and isinstance(incoming, dict):
        merged = dict(existing)
        for key, value in incoming.items():
            merged[key] = _merge_leaf(merged[key], value) if key in merged else value
        return merged
    if isinstance(existing, list) and isinstance(incoming, list):
        return incoming + existing
    return existing


def _resolve_includes(raw: dict[str, Any], *, _seen: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Expands `include` (a profile name, or a list of them) into a plain
    dict with no `include` key left in it -- recipe[index] keeps winning
    over any included value, per _merge_leaf. Included profiles are
    resolved recursively (an include can itself include), reversed
    ([::-1]) so that, among multiple includes providing the same
    not-otherwise-set key, the *last*-listed one wins: each earlier include
    is processed after (so its fill-ins find the key already claimed by a
    later one, per _merge_leaf's "existing wins" rule) -- while list entries
    still end up concatenated in the declared include order, since each
    earlier include's items get prepended in front of what the later ones
    already contributed."""
    includes = raw.get("include")
    if not includes:
        return raw
    if isinstance(includes, str):
        includes = [includes]

    resolved = {key: value for key, value in raw.items() if key != "include"}
    for name in reversed(includes):
        if name in _seen:
            raise RuntimeError(f"circular include: '{name}'")
        included = _resolve_includes(_load_profile_yaml(name), _seen=_seen | {name})
        for key, value in included.items():
            resolved[key] = _merge_leaf(resolved[key], value) if key in resolved else value
    return resolved


def load_profile(name: str) -> dict[str, Any]:
    return _resolve_includes(_load_profile_yaml(name))


def cmd_mod(args: argparse.Namespace) -> None:
    path = profile_path(args.profile_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        path.write_text("{}\n", encoding="utf-8")
    editor = os.environ.get("EDITOR", "vim")
    subprocess.call([editor, str(path)])


def cmd_ls(_args: argparse.Namespace) -> None:
    for name in list_profile_names():
        print(name)


def cmd_get(args: argparse.Namespace) -> None:
    profile = load_profile(args.profile_name)
    print(yaml.dump(profile, sort_keys=False, default_flow_style=False, allow_unicode=True), end="")


def cmd_rm(args: argparse.Namespace) -> None:
    path = profile_path(args.profile_name)
    if not path.is_file():
        print(f"ERROR: profile '{args.profile_name}' not found at {path}", file=sys.stderr)
        sys.exit(1)
    path.unlink()
    print(f"Deleted profile '{args.profile_name}' at {path}")


def cmd_mv(args: argparse.Namespace) -> None:
    old_path = profile_path(args.old_name)
    if not old_path.is_file():
        print(f"ERROR: profile '{args.old_name}' not found at {old_path}", file=sys.stderr)
        sys.exit(1)
    new_path = profile_path(args.new_name)
    if new_path.is_file():
        print(f"ERROR: profile '{args.new_name}' already exists at {new_path}", file=sys.stderr)
        sys.exit(1)

    new_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.rename(new_path)
    print(f"Renamed profile '{args.old_name}' to '{args.new_name}' at {new_path}")

    old_secrets = secrets_path(args.old_name)
    if old_secrets.is_file():
        new_secrets = secrets_path(args.new_name)
        new_secrets.parent.mkdir(parents=True, exist_ok=True)
        old_secrets.rename(new_secrets)
        print(f"Moved secrets to {new_secrets}")


def main() -> None:
    args = parse_args(sys.argv[1:])
    handlers: dict[str, Callable[[argparse.Namespace], None]] = {
        "mod": cmd_mod,
        "ls": cmd_ls,
        "get": cmd_get,
        "rm": cmd_rm,
        "mv": cmd_mv,
    }
    handlers[args.subcommand](args)


if __name__ == "__main__":
    main()
