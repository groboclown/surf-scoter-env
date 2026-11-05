#SPDX:MIT
"""The host file: where each of a configuration's host command programs is.

    {
        "$schema": "surf-scoter-host-file-1",
        "programs": {
            "aws": {"path": "/usr/local/bin/aws", "real": "/opt/aws-cli/bin/aws",
                    "origin": "resolved", "sha256": null}
        }
    }

It sits beside the configuration's `files/` (`host.json`), never among them,
and is never pushed to the project.  `programs` is keyed by the program as
the project file names it (a command's `program`, or its `command`).  Each
entry is the absolute `path` to run, the `real` file it was (symlinks
resolved), its `origin` (`resolved` by the tools, or written by the
developer, `user`), and an optional `sha256` of its contents.

The generator reads it (`gen-scoter-uc --host-file`), checks it, and puts it
in the universal configuration file's `host-file`, building `host-brokered`
from it; the checks then read it from there (`checks.check_with_host_file`).
This module makes it: `resolve` finds the programs, through the path checks
every time (VER-31), keeping what the developer wrote.
"""
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from . import checks, fileutil, jdata
from .err import ConfigErr
from .user_config import UserConfig

SCHEMA = "surf-scoter-host-file-1"
FILE_NAME = "host.json"
MAX_FILE_BYTES = 1024 * 1024

_KEYS = {"$schema", "programs"}
_ENTRY_KEYS = {"path", "real", "origin", "sha256"}
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")


class Origin(StrEnum):
    """Where an entry's path came from."""

    RESOLVED = "resolved"
    USER = "user"


def check(contents: jdata.JsonData, where: str) -> jdata.DictJsonData:
    """`contents`, if it's a host file; a `ConfigErr` naming `where` if not.

    The same rules the generator applies, so a file this accepts, it does too.
    """
    def bad(key: str, message: str) -> ConfigErr:
        return ConfigErr(f"{where}: {key}: {message}" if key else f"{where}: {message}")

    if not isinstance(contents, dict):
        raise bad("", "must be a JSON object")
    if contents.get("$schema") != SCHEMA:
        raise bad("$schema", f"must be `{SCHEMA}`; this isn't a host file this version reads")
    for key in sorted(set(contents) - _KEYS):
        raise bad(key, "unknown key")
    programs = contents.get("programs")
    if programs is None:
        return contents
    if not isinstance(programs, dict):
        raise bad("programs", "must be a mapping")
    for name, entry in programs.items():
        at = f"programs.{name}"
        if not name:
            raise bad(at, "must be the program as a command names it")
        if not isinstance(entry, dict):
            raise bad(at, "must be a mapping with `path`, `real` and `origin`")
        for key in sorted(set(entry) - _ENTRY_KEYS):
            raise bad(f"{at}.{key}", "unknown key")
        for key in ("path", "real"):
            value = entry.get(key)
            if not isinstance(value, str) or not os.path.isabs(value):
                raise bad(f"{at}.{key}", "must be an absolute path")
        if entry.get("origin") not in set(Origin):
            raise bad(f"{at}.origin", "must be `resolved` or `user`")
        sha256 = entry.get("sha256")
        if sha256 is not None and not (isinstance(sha256, str) and _SHA256.fullmatch(sha256)):
            raise bad(f"{at}.sha256", "must be a SHA-256, in hex, or null")
    return contents


def read(path: Path) -> jdata.DictJsonData:
    """The host file at `path`, checked; never through a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | fileutil.O_NOFOLLOW | fileutil.O_BINARY)
    except OSError as err:
        raise ConfigErr(f"{path}: can't be read; it may be a symlink ({err})") from err
    with os.fdopen(fd, "rb") as inp:
        data = inp.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ConfigErr(f"{path}: larger than the {MAX_FILE_BYTES} bytes allowed")
    try:
        contents = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ConfigErr(f"{path}: not a host file ({err})") from err
    return check(contents, str(path))


def write(directory: Path, contents: jdata.DictJsonData) -> Path:
    """Write the host file into `directory` (as `host.json`), private to the user."""
    check(contents, FILE_NAME)
    text = json.dumps(contents, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    return fileutil.write_private(directory, [FILE_NAME], text.encode("ascii"))


def empty() -> jdata.DictJsonData:
    """A host file with no programs."""
    return {"$schema": SCHEMA, "programs": {}}


@dataclass
class Resolution:
    """A host file made for the configuration in a universal configuration file."""

    contents: jdata.DictJsonData
    # The checks on the commands, with their programs as the host file has them.
    report: checks.Report
    # How it differs from the host file it was made from, for the review.
    changes: list[str] = field(default_factory=list)


def resolve(
    config: UserConfig, path_env: str | None = None, previous: jdata.DictJsonData | None = None,
) -> Resolution:
    """The host file for the configuration's commands, with the checks on them.

    Each program the commands run is found as the review finds it (the
    setup's `command-map`, then `path_env`), and every resolution runs the
    path checks (VER-31): a program in the project, one others can change, or
    one the setup's `paths` checks match is a finding.  An entry `previous`
    has with `origin: user` is kept as the developer wrote it, and checked
    the same way.  A program that isn't found has no entry, and is refused
    in the report.  Entries for programs no command runs any more are dropped.
    """
    if path_env is None:
        path_env = os.environ.get("PATH", "")
    kept = {
        name: entry
        for name, entry in (jdata.find_dict(previous or {}, "programs") or {}).items()
        if isinstance(entry, dict) and entry.get("origin") == Origin.USER
    }
    resolved: list[checks.Resolved] = []
    for r in checks.resolve_programs(config, path_env):
        entry = kept.get(r.name)
        if entry is None:
            resolved.append(r)
            continue
        path = jdata.req_str(entry, "path")
        present = os.path.isfile(path) and os.access(path, os.X_OK)
        resolved.append(checks.Resolved(
            r.index, r.command, r.name, path if present else None, "host-file:user",
        ))
    # The checks on the commands and where their programs are, not the files.
    findings = checks.check_host_commands(config, resolved)
    findings.extend(checks.check_program_paths(config, resolved, path_env))
    report = checks.Report(resolved, findings)
    programs: dict[str, jdata.JsonData] = {}
    for r in resolved:
        if r.name in programs:
            continue
        entry = kept.get(r.name)
        if entry is not None:
            programs[r.name] = dict(entry)
        elif r.executable is not None:
            programs[r.name] = {
                "path": r.executable,
                "real": os.path.realpath(r.executable),
                "origin": str(Origin.RESOLVED),
                "sha256": None,
            }
    contents = {"$schema": SCHEMA, "programs": programs}
    return Resolution(contents, report, changes(previous, contents))


def changes(before: jdata.DictJsonData | None, after: jdata.DictJsonData) -> list[str]:
    """How the host file `after` differs from `before`, a line for each program."""
    def programs(data: jdata.DictJsonData | None) -> dict[str, jdata.DictJsonData]:
        found = jdata.find_dict(data or {}, "programs") or {}
        return {name: entry for name, entry in found.items() if isinstance(entry, dict)}

    old, new = programs(before), programs(after)
    lines: list[str] = []
    for name in sorted(set(old) | set(new)):
        was, now = old.get(name), new.get(name)
        if was is None and now is not None:
            lines.append(f"`{name}`: added, runs {now.get('path')}")
        elif now is None and was is not None:
            lines.append(f"`{name}`: removed (it ran {was.get('path')})")
        elif was is not None and now is not None:
            lines.extend(_entry_changes(name, was, now))
    return lines


def _entry_changes(name: str, was: jdata.DictJsonData, now: jdata.DictJsonData) -> Sequence[str]:
    if was.get("path") != now.get("path"):
        return [f"`{name}`: runs {now.get('path')}, not {was.get('path')}"]
    if was.get("real") != now.get("real"):
        return [f"`{name}`: {now.get('path')} is now {now.get('real')}, not {was.get('real')}"]
    return []
