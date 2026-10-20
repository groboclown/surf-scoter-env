#SPDX:MIT
"""Snapshots of a project's files: a manifest, with the contents kept as blobs.

A manifest lists each file's path (relative to the directory it was taken
from), its SHA-256, its size, whether it's executable, and for formats the
tools understand, a canonical hash: the hash of the file's meaning, without
its comments and blank lines.  The manifest's own hash is the snapshot's id;
`meaning_id` is the same, made from the canonical hashes where there are some,
so two snapshots that differ only in comments have the same `meaning_id`.

Nothing is ever extracted from an archive: a snapshot is restored by
writing each file the manifest lists, after checking its path.
"""
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .. import fileutil, jdata
from ..err import VerifiedStoreErr
from .store import VerifiedStore, canonical_json, check_private, json_id, nullable_str

DEV_ENV_FILE = "dev-env.yaml"

_O_NONBLOCK: int = getattr(os, "O_NONBLOCK", 0)


class SnapshotErr(VerifiedStoreErr):
    """A project file can't go into a snapshot."""


@dataclass(frozen=True)
class SnapshotLimits:
    """The largest file, and the most for all files, in bytes."""

    max_file_size: int = 16 * 1024 * 1024
    max_snapshot_size: int = 256 * 1024 * 1024


@dataclass(frozen=True)
class FileEntry:
    """One file in a manifest."""

    sha256: str
    size: int
    executable: bool
    canonical: str | None = None

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data."""
        return {
            "sha256": self.sha256,
            "size": self.size,
            "executable": self.executable,
            "canonical": self.canonical,
        }

    @staticmethod
    def from_json(data: jdata.JsonData) -> "FileEntry":
        """From JSON data."""
        size = jdata.req_int(data, "size")
        if size < 0:
            raise ValueError("size: must not be negative")
        return FileEntry(
            sha256=jdata.req_str(data, "sha256"),
            size=size,
            executable=jdata.req_bool(data, "executable"),
            canonical=nullable_str(data, "canonical"),
        )


@dataclass(frozen=True)
class Manifest:
    """The files of a snapshot, by path."""

    files: dict[str, FileEntry]

    @property
    def id(self) -> str:
        """The snapshot's id: the hash of its files, exactly."""
        return json_id({path: entry.to_json() for path, entry in self.files.items()})

    @property
    def meaning_id(self) -> str:
        """The hash of what the files mean: the same for files that differ only in noise."""
        return json_id({
            path: {
                "meaning": entry.canonical or "sha256:" + entry.sha256,
                "executable": entry.executable,
            }
            for path, entry in self.files.items()
        })

    def size(self) -> int:
        """All the files' sizes."""
        return sum(entry.size for entry in self.files.values())

    def changed(self, other: "Manifest") -> list[str]:
        """The paths whose meaning differs between this manifest and `other`."""
        def meaning(entry: FileEntry | None) -> tuple[str, bool] | None:
            if entry is None:
                return None
            return (entry.canonical or "sha256:" + entry.sha256, entry.executable)
        paths = set(self.files) | set(other.files)
        return sorted(p for p in paths if meaning(self.files.get(p)) != meaning(other.files.get(p)))

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data, with its id."""
        return {
            "id": self.id,
            "files": {path: entry.to_json() for path, entry in sorted(self.files.items())},
        }

    @staticmethod
    def from_json(data: jdata.JsonData) -> "Manifest":
        """From JSON data, checking every path and the id."""
        files: dict[str, FileEntry] = {}
        for key in jdata.opt_dict_keys(data, "files"):
            path = str(key[-1])
            check_relative(path)
            files[path] = FileEntry.from_json(jdata.req_dict(data, *key))
        manifest = Manifest(files)
        if jdata.req_str(data, "id") != manifest.id:
            raise ValueError("id: doesn't match the files")
        return manifest


@dataclass
class Snapshot:
    """A manifest, with the contents read for it."""

    manifest: Manifest
    contents: dict[str, bytes]
    warnings: list[str] = field(default_factory=list)

    def store(self, store: VerifiedStore) -> Manifest:
        """Keep the contents as blobs in `store` (which must be locked)."""
        for path, entry in self.manifest.files.items():
            digest = store.put_blob(self.contents[path])
            if digest != entry.sha256:
                raise SnapshotErr(f"{path}: changed while it was stored")
        return self.manifest


@dataclass(frozen=True)
class ParsedFiles:
    """The project file and dev container, as the generator parsed them.

    They're in the SUC (`project` and `dev-container`).  The tools have no
    YAML parser, so these give the YAML files their canonical hashes: the
    project file's is its parsed data's, and every `dev-env.yaml`'s is the
    merged dev container's (a change to a parent that the merge hides
    changes nothing).
    """

    project: jdata.JsonData = None
    dev_container: jdata.JsonData = None


# ---------------------------------------------------------------------
# Paths


def check_relative(path: str) -> None:
    """Refuse a manifest path that could lead outside where it's restored."""
    parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or any(part in ("", ".", "..") for part in parts)
        or any(c in path for c in ("\\", "\0", ":"))
    ):
        raise SnapshotErr(f"`{path}` is not a relative path inside a snapshot")


# ---------------------------------------------------------------------
# Canonical forms


_DIRECTIVE = re.compile(r"#\s*([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(\S.*?)\s*")
_HEREDOC = re.compile(r"<<(-?)([\"']?)([A-Za-z_][A-Za-z0-9_]*)\2")


def is_dockerfile(name: str) -> bool:
    """Whether the file name is a Dockerfile's."""
    return name == "Dockerfile" or name.startswith("Dockerfile.") or name.endswith(".Dockerfile")


def canonical_dockerfile(text: str) -> str:
    """The Dockerfile without what doesn't change the build.

    Comment lines and blank lines go, and trailing whitespace, except inside
    heredocs (`RUN <<EOF`), whose lines are the script and stay as written.
    The parser directives at the top (`# syntax=...`, `# escape=...`) stay.
    Indentation stays: inside a quoted string spanning lines, it's part of
    the string.
    """
    lines = text.splitlines()
    out: list[str] = []
    escape = "\\"
    i = 0
    # Parser directives come first, before any other line.
    while i < len(lines):
        match = _DIRECTIVE.fullmatch(lines[i].strip())
        if not match:
            break
        key, value = match.group(1).lower(), match.group(2)
        if key == "escape" and value in ("\\", "`"):
            escape = value
        out.append(f"# {key}={value}")
        i += 1
    heredocs: list[tuple[str, bool]] = []
    starting: list[tuple[str, bool]] = []
    for line in lines[i:]:
        if heredocs:
            out.append(line)
            word, strip_tabs = heredocs[0]
            if (line.lstrip("\t") if strip_tabs else line) == word:
                heredocs.pop(0)
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        line = line.rstrip()
        out.append(line)
        starting.extend((m.group(3), m.group(1) == "-") for m in _HEREDOC.finditer(line))
        # Heredocs start once the instruction's last line is read.
        if not line.endswith(escape):
            heredocs, starting = starting, []
    return "\n".join(out) + "\n"


def _no_duplicates(pairs: list[tuple[str, jdata.JsonData]]) -> dict[str, jdata.JsonData]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate keys")
    return dict(pairs)


def _no_constants(name: str) -> None:
    raise ValueError(f"{name} isn't JSON")


def canonical_hash(rel_path: str, data: bytes, parsed: ParsedFiles, project_file: str) -> str | None:
    """The canonical hash of the file at `rel_path`, or None for bytes only."""
    name = rel_path.rsplit("/", 1)[-1]
    if rel_path == project_file and parsed.project is not None:
        return json_id(parsed.project)
    if name == DEV_ENV_FILE and parsed.dev_container is not None:
        return json_id(parsed.dev_container)
    if is_dockerfile(name):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return "sha256:" + hashlib.sha256(canonical_dockerfile(text).encode("utf-8")).hexdigest()
    if name.endswith(".json"):
        try:
            value = json.loads(
                data.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constants,
            )
        except (UnicodeDecodeError, ValueError):
            return None
        return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()
    return None


# ---------------------------------------------------------------------
# Taking a snapshot


class _Reader:
    """Reads files into a snapshot, within its limits."""

    def __init__(self, limits: SnapshotLimits) -> None:
        self.limits = limits
        self.total = 0
        self.contents: dict[str, bytes] = {}
        self.executable: dict[str, bool] = {}
        self.warnings: list[str] = []

    def read(self, path: Path, rel: str, *, links_ok: bool) -> None:
        """Read the file."""
        check_relative(rel)
        flags = os.O_RDONLY | fileutil.O_NOFOLLOW | fileutil.O_BINARY | _O_NONBLOCK
        try:
            fd = os.open(path, flags)
        except OSError as err:
            raise SnapshotErr(f"{path}: can't be read; it may be a symlink ({err})") from err
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise SnapshotErr(f"{path}: not a regular file")
            if st.st_nlink > 1:
                if not links_ok:
                    raise SnapshotErr(
                        f"{path}: has other hard links; a container directory can't share "
                        f"files with anything outside it"
                    )
                self.warnings.append(f"{path}: has other hard links")
            limit = self.limits.max_file_size
            if st.st_size > limit:
                raise SnapshotErr(f"{path}: larger than the {limit} bytes allowed")
            with os.fdopen(fd, "rb", closefd=False) as inp:
                data = inp.read(limit + 1)
            if len(data) > limit:
                raise SnapshotErr(f"{path}: larger than the {limit} bytes allowed")
        finally:
            os.close(fd)
        self.total += len(data)
        if self.total > self.limits.max_snapshot_size:
            raise SnapshotErr(
                f"{path}: the project's files are larger than the "
                f"{self.limits.max_snapshot_size} bytes allowed"
            )
        self.contents[rel] = data
        self.executable[rel] = bool(st.st_mode & stat.S_IXUSR)

    def read_dir(self, directory: Path, rel: str) -> None:
        """Every file in `directory`, refusing links and special files."""
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as err:
            raise SnapshotErr(f"{directory}: can't be read ({err})") from err
        for entry in entries:
            path = Path(entry.path)
            child = f"{rel}/{entry.name}" if rel else entry.name
            if entry.is_symlink():
                raise SnapshotErr(f"{path}: a symlink; a container directory can't have any")
            if entry.is_dir(follow_symlinks=False):
                self.read_dir(path, child)
            elif entry.is_file(follow_symlinks=False):
                self.read(path, child, links_ok=False)
            else:
                raise SnapshotErr(f"{path}: a special file; a container directory can't have any")

    def snapshot(self, parsed: ParsedFiles, project_file: str) -> Snapshot:
        files = {
            rel: FileEntry(
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
                executable=self.executable[rel],
                canonical=canonical_hash(rel, data, parsed, project_file),
            )
            for rel, data in sorted(self.contents.items())
        }
        return Snapshot(Manifest(files), dict(self.contents), list(self.warnings))


def snapshot_project(
    files_dir: Path,
    project_file_name: str,
    container_dirs: Iterable[Path],
    parsed: ParsedFiles,
    limits: SnapshotLimits,
) -> Snapshot:
    """The project file, and the container directories inside `files_dir`.

    `container_dirs` are the dev container's directory and its parents'
    (the SUC's `dev-container-path` and `parent-builds`); those outside
    `files_dir` aren't the project's, and are left out.  A hard-linked
    project file only warns; anything but plain files in a container
    directory is refused.
    """
    files_dir = Path(os.path.realpath(files_dir))
    reader = _Reader(limits)
    reader.read(files_dir / project_file_name, project_file_name, links_ok=True)
    for directory in container_dirs:
        real = Path(os.path.realpath(directory))
        if not real.is_relative_to(files_dir):
            continue
        rel = real.relative_to(files_dir).as_posix()
        if rel == ".":
            raise SnapshotErr(
                f"{directory}: a container directory can't be the project files directory itself"
            )
        reader.read_dir(real, rel)
    return reader.snapshot(parsed, project_file_name)


def snapshot_dir(directory: Path, limits: SnapshotLimits) -> Snapshot:
    """Every file in a container directory outside the project (`dev-containers-verified`)."""
    reader = _Reader(limits)
    reader.read_dir(Path(os.path.realpath(directory)), "")
    return reader.snapshot(ParsedFiles(), "")


def container_dirs(dev_container_path: Path | None, parent_dirs: Sequence[Path]) -> list[Path]:
    """The directories a dev container builds from: its own, then its parents'."""
    return ([dev_container_path] if dev_container_path else []) + list(parent_dirs)


# ---------------------------------------------------------------------
# Restoring


def restore(store: VerifiedStore, manifest: Manifest, dest: Path, limits: SnapshotLimits) -> None:
    """Write the manifest's files into `dest`: an empty directory only the user can use.

    Each blob is checked against its hash and size as it's read, and each
    path before it's written; nothing is written outside `dest`.
    """
    st = os.lstat(dest)
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise SnapshotErr(f"{dest}: not a directory")
    check_private(st, dest)
    if os.listdir(dest):
        raise SnapshotErr(f"{dest}: not empty")
    for rel, entry in sorted(manifest.files.items()):
        check_relative(rel)
        if entry.size > limits.max_file_size:
            raise SnapshotErr(f"{rel}: larger than the {limits.max_file_size} bytes allowed")
        data = store.get_blob(entry.sha256, entry.size)
        if len(data) != entry.size:
            raise SnapshotErr(f"{rel}: its blob isn't the size the manifest says")
        target = dest
        for part in rel.split("/")[:-1]:
            target = target / part
            if not os.path.lexists(target):
                target.mkdir(mode=0o700)
            elif os.path.islink(target) or not target.is_dir():
                raise SnapshotErr(f"{target}: not a directory")
        target = target / rel.rsplit("/", 1)[-1]
        mode = 0o700 if entry.executable else 0o600
        fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | fileutil.O_NOFOLLOW | fileutil.O_BINARY,
            mode,
        )
        with os.fdopen(fd, "wb") as out:
            out.write(data)
