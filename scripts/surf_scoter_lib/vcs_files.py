#SPDX:MIT
"""Asking the project's VCS about its committed files, for the pull.

A VCS section (`NAME-vcs`) answers through three `runs`, each given
`{x:root}` (your repository, never the project clone), and a directory in it,
`{x:path}`, relative to the root:

* `list-files` (`{x:ref}`): the files under the directory at a ref, and the
  commit the ref is (`surf-scoter-vcs-files-1`);
* `show-file` (`{x:commit}`, with `{x:path}` a file): a committed file's
  contents, from that commit;
* `file-status` (`{x:ref}`, `{x:files}`): for each file in a copy of the
  directory, whether it's committed (`surf-scoter-vcs-file-status-1`).

Whatever a handler prints is untrusted (VER-30): every command's output is
cut off at a limit, every report is checked against its schema, and every
path passes the snapshot's path and name checks.  A VCS without
`file-status` reports `unknown` for every file; one without `list-files`
and `show-file` can't be pulled from a commit.
"""
import hashlib
import json
import os
import re
import subprocess  # nosec: B404  # Runs the setup's VCS commands, as lists.
import tempfile
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from . import jdata
from .err import ConfigErr
from .user_config import UserConfig
from .verified import snapshot

FILES_SCHEMA = "surf-scoter-vcs-files-1"
STATUS_SCHEMA = "surf-scoter-vcs-file-status-1"
# The most a listing or status report may be, in bytes.
MAX_REPORT_BYTES = 4 * 1024 * 1024
TIMEOUT_SECONDS = 120
# A commit or object id: SHA-1 or SHA-256, in hex.
OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class VcsErr(ConfigErr):
    """The VCS couldn't answer, or answered with something it shouldn't."""


class Kind(StrEnum):
    """What a committed entry is."""

    FILE = "file"
    EXECUTABLE = "executable"
    SYMLINK = "symlink"
    SUBMODULE = "submodule"


class Status(StrEnum):
    """Whether a file is as committed."""

    COMMITTED = "committed"
    MODIFIED = "modified"
    UNTRACKED = "untracked"
    IGNORED = "ignored"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Entry:
    """A committed file, as `list-files` gives it."""

    kind: Kind
    id: str
    size: int | None


@dataclass(frozen=True)
class Listing:
    """The files under a directory at a commit, by path relative to it."""

    commit: str
    files: dict[str, Entry]


@dataclass(frozen=True)
class FileStatus:
    """A file's status, and the committed version's id, if there is one."""

    status: Status
    id: str | None


@dataclass(frozen=True)
class StatusReport:
    """Each file's status against a commit (None when the VCS can't tell)."""

    commit: str | None
    files: dict[str, FileStatus]

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data, for the provenance."""
        return {
            "commit": self.commit,
            "files": {
                path: {"status": str(s.status), "id": s.id} for path, s in sorted(self.files.items())
            },
        }


# ---------------------------------------------------------------------
# Running the handler


def can_show(config: UserConfig, vcs: str) -> bool:
    """Whether the VCS can give committed files (`list-files` and `show-file`)."""
    section = config.get_vcs_section_for(vcs)
    return config.has_run(section, "list-files") and config.has_run(section, "show-file")


def _where(config: UserConfig, vcs: str, run: str) -> str:
    """The VCS's `run`, for messages."""
    return f"`{config.get_vcs_section_for(vcs)}: runs: {run}`"


def run_action(config: UserConfig, vcs: str, run: str, values: dict[str, str], max_bytes: int) -> bytes:
    """Run the VCS's `run` with `values`; what it printed, which must be at most `max_bytes`.

    The command and its environment are the setup's (`require_run`, with each
    value a whole argument, and `command_env`).  Unlike `run_command`, the
    output is read only up to the limit, and a handler that stops answering
    is killed.
    """
    section = config.get_vcs_section_for(vcs)
    where = _where(config, vcs, run)
    cmd = config.require_run(section, run, values, whole_args=True)
    with tempfile.TemporaryFile() as err_out:
        try:
            proc = subprocess.Popen(  # nosec: B603  # The setup's command, as a list.
                cmd, stdout=subprocess.PIPE, stderr=err_out, stdin=subprocess.DEVNULL,
                shell=False, env=config.command_env(section),
            )
        except OSError as err:
            raise VcsErr(f"{where} couldn't run: {err}") from err
        assert proc.stdout is not None  # nosec: B101  # piped above.
        # Reading blocks until it ends, so it's killed if it takes too long.
        timer = threading.Timer(TIMEOUT_SECONDS, proc.kill)
        timer.start()
        try:
            data = proc.stdout.read(max_bytes + 1)
            if len(data) > max_bytes:
                proc.kill()
                raise VcsErr(f"{where} printed more than {max_bytes} bytes")
            code = proc.wait()
        finally:
            timer.cancel()
            proc.stdout.close()
            proc.wait()
        if code < 0:
            raise VcsErr(f"{where} was stopped, or took longer than {TIMEOUT_SECONDS} seconds")
        if code != 0:
            err_out.seek(0)
            message = err_out.read(4096).decode("utf-8", errors="replace").strip()
            raise VcsErr(f"{where} failed: {message!a}")
    return data


def _report(data: bytes, schema: str, where: str) -> jdata.DictJsonData:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise VcsErr(f"{where}: not JSON ({err})") from err
    if not isinstance(value, dict) or value.get("$schema") != schema:
        raise VcsErr(f"{where}: not a `{schema}` report")
    extra = set(value) - {"$schema", "commit", "files"}
    if extra:
        raise VcsErr(f"{where}: unknown keys {sorted(extra)!a}")
    return value


def _path(path: str, where: str) -> str:
    try:
        # The name first, so an error never prints one as it is.
        snapshot.check_name(path)
        snapshot.check_relative(path)
    except snapshot.SnapshotErr as err:
        raise VcsErr(f"{where}: {err}") from err
    return path


def _object_id(value: jdata.JsonData, key: str, where: str) -> str:
    """The id at `key`, which must be one."""
    found = jdata.find_str(value, key)
    if found is None or not OBJECT_ID.fullmatch(found):
        raise VcsErr(f"{where}: `{key}` isn't an object id")
    return found


def repo_path(root: Path, directory: Path) -> str:
    """`directory`, relative to the repository `root`, as the handlers take it."""
    real_root = Path(os.path.realpath(root))
    real = Path(os.path.realpath(directory))
    if not real.is_relative_to(real_root) or real == real_root:
        raise VcsErr(f"{directory} isn't a directory inside the repository at {root}")
    return _path(real.relative_to(real_root).as_posix(), str(directory))


# ---------------------------------------------------------------------
# The questions


def list_files(config: UserConfig, vcs: str, root: Path, ref: str, path: str) -> Listing:
    """The files under `path` at `ref`, checked."""
    where = _where(config, vcs, "list-files")
    value = _report(
        run_action(config, vcs, "list-files", {"root": str(root), "ref": ref, "path": path}, MAX_REPORT_BYTES),
        FILES_SCHEMA, where,
    )
    files: dict[str, Entry] = {}
    try:
        for key in jdata.find_dict_keys(value, "files"):
            at = f"{where}: {_path(str(key[-1]), where)}"
            if set(jdata.req_dict(value, *key)) != {"kind", "id", "size"}:
                raise VcsErr(f"{at}: must have `kind`, `id` and `size`")
            kind = jdata.req_str(value, *key, "kind")
            if kind not in set(Kind):
                raise VcsErr(f"{at}: `kind` must be one of {sorted(Kind)}")
            size = jdata.find_int(value, *key, "size")
            if size is not None and size < 0:
                raise VcsErr(f"{at}: `size` must be a size, or null")
            files[str(key[-1])] = Entry(Kind(kind), _object_id(jdata.req_dict(value, *key), "id", at), size)
        return Listing(_object_id(value, "commit", where), files)
    except (KeyError, TypeError) as err:
        raise VcsErr(f"{where}: {err}") from err


def show_file(config: UserConfig, vcs: str, root: Path, commit: str, path: str, max_bytes: int) -> bytes:
    """A committed file's contents, at most `max_bytes`."""
    if not OBJECT_ID.fullmatch(commit):
        raise VcsErr(f"{commit!a} isn't a commit id")
    return run_action(config, vcs, "show-file", {"root": str(root), "commit": commit, "path": path}, max_bytes)


def git_blob_id(data: bytes, commit: str) -> str:
    """Git's id for a blob holding `data`, in the hash its commit ids use."""
    digest = hashlib.sha256() if len(commit) == 64 else hashlib.sha1(usedforsecurity=False)
    digest.update(b"blob %d\0" % len(data) + data)
    return digest.hexdigest()


def snapshot_commit(
    config: UserConfig, vcs: str, root: Path, ref: str, files_dir: Path, limits: snapshot.SnapshotLimits,
) -> tuple[str, snapshot.Snapshot]:
    """The files directory as committed at `ref`: the commit, and the source snapshot.

    The seeding counterpart of `snapshot.snapshot_source`, with the same
    refusals: a symlink or submodule is refused, as a link on disk is, and
    the limits apply to the listing's sizes before anything is read, and to
    each file as it's read.  Every file comes from the one commit the listing
    names; for Git, each one's contents must hash to the id it listed.
    """
    path = repo_path(root, files_dir)
    listing = list_files(config, vcs, root, ref, path)
    total = 0
    for rel, entry in listing.files.items():
        if entry.kind in (Kind.SYMLINK, Kind.SUBMODULE):
            raise VcsErr(f"{rel}: a {entry.kind}; the project's files can't have any")
        total += entry.size or 0
        if (entry.size or 0) > limits.max_file_size:
            raise VcsErr(f"{rel}: larger than the {limits.max_file_size} bytes allowed")
    if total > limits.max_snapshot_size:
        raise VcsErr(f"the project's files are larger than the {limits.max_snapshot_size} bytes allowed")
    is_git = vcs == "git"
    entries: list[tuple[str, bytes, bool]] = []
    for rel, entry in sorted(listing.files.items()):
        data = show_file(config, vcs, root, listing.commit, f"{path}/{rel}", limits.max_file_size)
        if entry.size is not None and len(data) != entry.size:
            raise VcsErr(f"{rel}: its contents aren't the size the listing says")
        if is_git and git_blob_id(data, listing.commit) != entry.id:
            raise VcsErr(f"{rel}: its contents aren't the ones the listing names")
        entries.append((rel, data, entry.kind == Kind.EXECUTABLE))
    return listing.commit, snapshot.snapshot_bytes(entries, limits)


def file_status(
    config: UserConfig, vcs: str, root: Path, ref: str, files_dir: Path, copy: Path,
) -> StatusReport:
    """Whether each file in `copy` (a copy of `files_dir`'s files) is as committed at `ref`.

    A VCS without `file-status` reports `unknown` for each.  The report must
    name exactly the copy's files.
    """
    present = sorted(
        Path(directory, name).relative_to(copy).as_posix()
        for directory, _, names in os.walk(copy)
        for name in names
    )
    if not config.has_run(config.get_vcs_section_for(vcs), "file-status"):
        return StatusReport(None, {rel: FileStatus(Status.UNKNOWN, None) for rel in present})
    where = _where(config, vcs, "file-status")
    values = {"root": str(root), "ref": ref, "path": repo_path(root, files_dir), "files": str(copy)}
    value = _report(run_action(config, vcs, "file-status", values, MAX_REPORT_BYTES), STATUS_SCHEMA, where)
    files: dict[str, FileStatus] = {}
    try:
        for key in jdata.find_dict_keys(value, "files"):
            at = f"{where}: {_path(str(key[-1]), where)}"
            entry = jdata.req_dict(value, *key)
            if set(entry) != {"status", "id"}:
                raise VcsErr(f"{at}: must have `status` and `id`")
            status = jdata.req_str(entry, "status")
            if status not in set(Status):
                raise VcsErr(f"{at}: `status` must be one of {sorted(Status)}")
            object_id = None if entry["id"] is None else _object_id(entry, "id", at)
            files[str(key[-1])] = FileStatus(Status(status), object_id)
        commit = _object_id(value, "commit", where)
    except (KeyError, TypeError) as err:
        raise VcsErr(f"{where}: {err}") from err
    if sorted(files) != present:
        raise VcsErr(f"{where}: doesn't report exactly the copy's files")
    return StatusReport(commit, files)
