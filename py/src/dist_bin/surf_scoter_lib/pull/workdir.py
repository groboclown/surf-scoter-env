#SPDX:MIT
"""The pull's working directories, and what's recorded about them.

    ${scoter:state-dir}/pull/                  0700
        work/ID/                               0700  the working directory
            files/                                   a copy of the source's files, nothing added
            host.json                                its host file, beside them (VER-42)
        records/ID/                            0700  outside the working directory (VER-25)
            provenance.json                    0600  written once, by `seed`
            analyses/AID.json                  0600  each analysis: the snapshot it names

The developer and the reviewer can change anything in the working
directory, so nothing in it says where it came from: the provenance and the
analyses are kept beside it, written only by `seed` and `analyze`.  A
working directory is found by its id, through the SUC: `open` refuses one
whose provenance was recorded under a different setup (VER-29).
"""
import datetime
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .. import fileutil, host_file, jdata
from ..err import ConfigErr
from ..user_config import UserConfig
from ..verified import snapshot
from ..verified.store import json_id, parse_time, time_text

PULL_DIR = "pull"
WORK_DIR = "work"
RECORDS_DIR = "records"
FILES_DIR = "files"
PROVENANCE_FILE = "provenance.json"
ANALYSES_DIR = "analyses"
PROVENANCE_SCHEMA = "surf-scoter-pull-provenance-1"
ANALYSIS_RECORD_SCHEMA = "surf-scoter-pull-analysis-record-1"
MAX_RECORD_BYTES = 64 * 1024 * 1024

_ID = re.compile(r"[a-z0-9_]{8,64}")
_ANALYSIS_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")


class PullErr(ConfigErr):
    """A pull's working directory, or what's recorded about it, can't be used."""


def check_id(workdir_id: str) -> str:
    """Refuse a working directory id that isn't one `create` makes."""
    if not _ID.fullmatch(workdir_id):
        raise PullErr(f"{workdir_id!a} isn't a working directory id")
    return workdir_id


def check_analysis_id(analysis_id: str) -> str:
    """Refuse an analysis id that isn't one `new_analysis_id` makes."""
    if not _ANALYSIS_ID.fullmatch(analysis_id):
        raise PullErr(f"{analysis_id!a} isn't an analysis id")
    return analysis_id


def new_analysis_id(now: datetime.datetime) -> str:
    """A new analysis id: the time, and a random part."""
    return now.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)


def fingerprint(config: UserConfig) -> str:
    """The setup a working directory is read under: its state directory, store and checks.

    `seed` records it, and every later phase must be given a SUC with the
    same, so a reviewer started with a different environment can't read
    another state directory, or judge by other checks (VER-29).
    """
    resolved = config.raw("resolved")
    sections = resolved if isinstance(resolved, dict) else {}
    return json_id({
        "state-dir": str(config.require_path("scoter", "state-dir")),
        "store": str(config.optional_path("verification", "store")),
        "host-command-checks": config.raw("host-command-checks"),
        "simple-checks": {
            name: {group: section.get(group) for group in ("project-files", "egress-hosts")}
            for name, section in sorted(sections.items())
            if isinstance(section, dict) and ("project-files" in section or "egress-hosts" in section)
        },
    })


def pull_root(config: UserConfig) -> Path:
    """`${scoter:state-dir}/pull`, private to the user."""
    return fileutil.private_dir(config.require_path("scoter", "state-dir"), [PULL_DIR])


@dataclass(frozen=True)
class Provenance:
    """Where a working directory came from, as `seed` found it."""

    workdir_id: str
    env_id: str
    project_dir: str
    # `project` (with `ref` and `commit`, or from the working tree), and later `unlocked` or `locked`.
    source: jdata.DictJsonData
    # The snapshot seeded: the build's files, as they came from the source.
    seeded: snapshot.Manifest
    # The VCS's report on the seeded files.
    vcs: jdata.DictJsonData
    # The paths that differ from the base (the last pull), or null without one.
    since_base: list[str] | None
    # Whether the seeded files and the setup's checks are as at the last pull.
    unchanged: bool
    fingerprint: str
    created_at: datetime.datetime
    warnings: list[str]
    # The id of the whole source snapshot, every file in it: a run compares
    # the project's files with it, by hash alone (VER-21).
    source_id: str | None = None

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data."""
        return {
            "$schema": PROVENANCE_SCHEMA,
            "id": self.workdir_id,
            "environment": self.env_id,
            "project-dir": self.project_dir,
            "source": self.source,
            "seeded": self.seeded.to_json(),
            "vcs": self.vcs,
            "since-base": self.since_base,
            "unchanged": self.unchanged,
            "fingerprint": self.fingerprint,
            "created-at": time_text(self.created_at),
            "warnings": list(self.warnings),
            "source-id": self.source_id,
        }

    @staticmethod
    def from_json(data: jdata.JsonData) -> "Provenance":
        """From JSON data."""
        since = jdata.find_list(data, "since-base")
        return Provenance(
            workdir_id=check_id(jdata.req_str(data, "id")),
            env_id=jdata.req_str(data, "environment"),
            project_dir=jdata.req_str(data, "project-dir"),
            source=jdata.req_dict(data, "source"),
            seeded=snapshot.Manifest.from_json(jdata.req_dict(data, "seeded")),
            vcs=jdata.req_dict(data, "vcs"),
            since_base=None if since is None else [jdata.req_str(p) for p in since],
            unchanged=jdata.req_bool(data, "unchanged"),
            fingerprint=jdata.req_str(data, "fingerprint"),
            created_at=parse_time(jdata.req_str(data, "created-at")),
            warnings=[jdata.req_str(w) for w in jdata.find_list(data, "warnings") or []],
            source_id=jdata.find_str(data, "source-id"),
        )


@dataclass(frozen=True)
class AnalysisRecord:
    """What an analysis looked at: the working directory's files and host file, by hash."""

    analysis_id: str
    files: snapshot.Manifest
    host_file: str

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data."""
        return {
            "$schema": ANALYSIS_RECORD_SCHEMA,
            "id": self.analysis_id,
            "files": self.files.to_json(),
            "host-file": self.host_file,
        }

    @staticmethod
    def from_json(data: jdata.JsonData) -> "AnalysisRecord":
        """From JSON data."""
        return AnalysisRecord(
            analysis_id=check_analysis_id(jdata.req_str(data, "id")),
            files=snapshot.Manifest.from_json(jdata.req_dict(data, "files")),
            host_file=jdata.req_str(data, "host-file"),
        )


@dataclass(frozen=True)
class WorkDir:
    """A registered working directory, opened under a matching setup."""

    path: Path
    records: Path
    provenance: Provenance

    @property
    def id(self) -> str:
        """Its id."""
        return self.provenance.workdir_id

    @property
    def files(self) -> Path:
        """The configuration's files: `files/`."""
        return self.path / FILES_DIR

    @property
    def host_file(self) -> Path:
        """The host file, beside the files."""
        return self.path / host_file.FILE_NAME

    def save_analysis(self, record: AnalysisRecord) -> None:
        """Record an analysis, beside the working directory."""
        _write(self.records / ANALYSES_DIR, record.analysis_id + ".json", record.to_json())

    def analysis(self, analysis_id: str) -> AnalysisRecord:
        """The analysis `analysis_id` of this working directory."""
        path = self.records / ANALYSES_DIR / (check_analysis_id(analysis_id) + ".json")
        data = _read(path)
        if data is None:
            raise PullErr(f"{analysis_id}: no such analysis of working directory {self.id}")
        try:
            record = AnalysisRecord.from_json(data)
        except (KeyError, TypeError, ValueError) as err:
            raise PullErr(f"{path}: {err}") from err
        if record.analysis_id != analysis_id:
            raise PullErr(f"{path}: is for analysis {record.analysis_id}")
        return record

    def snapshot(self, limits: snapshot.SnapshotLimits, project_file_name: str) -> tuple[snapshot.Snapshot, str]:
        """The working directory as it is now: its files, and its host file's hash.

        Read as seeding reads a source: links and special files refused, the
        limits applied.
        """
        taken = snapshot.snapshot_source(self.files, project_file_name, limits)
        return taken, host_file_hash(self.host_file)


def host_file_hash(path: Path) -> str:
    """The host file's hash: its contents, as read (never through a symlink)."""
    return json_id(host_file.read(path))


def create(config: UserConfig) -> tuple[str, Path, Path]:
    """A new, empty working directory: its id, where it is, and where its records go.

    Each pull gets its own (`mkdtemp`), so two pulls never share one (VER-35).
    """
    root = pull_root(config)
    work = fileutil.private_dir(root, [WORK_DIR])
    path = Path(tempfile.mkdtemp(dir=work, prefix=""))
    os.chmod(path, fileutil.MODE_USER_RWX)
    workdir_id = path.name.lower()
    if workdir_id != path.name or not _ID.fullmatch(workdir_id):
        shutil.rmtree(path)
        raise PullErr(f"{path}: an unexpected name for a working directory")
    records = fileutil.private_dir(root, [RECORDS_DIR, workdir_id])
    (path / FILES_DIR).mkdir(mode=fileutil.MODE_USER_RWX)
    return workdir_id, path, records


def save_provenance(records: Path, provenance: Provenance) -> None:
    """Write the provenance; only `seed` does, once."""
    if (records / PROVENANCE_FILE).exists():
        raise PullErr(f"{records}: already has a provenance")
    _write(records, PROVENANCE_FILE, provenance.to_json())


def open_workdir(config: UserConfig, workdir_id: str) -> WorkDir:
    """The registered working directory `workdir_id`, read through `config`.

    Refused when it isn't registered, or its provenance was recorded under a
    setup other than `config`'s (VER-29).
    """
    root = pull_root(config)
    check_id(workdir_id)
    path = root / WORK_DIR / workdir_id
    records = root / RECORDS_DIR / workdir_id
    data = _read(records / PROVENANCE_FILE)
    if data is None or not _is_private_dir(path):
        raise PullErr(f"{workdir_id}: no such working directory; `scoter pull seed` makes them")
    try:
        provenance = Provenance.from_json(data)
    except (KeyError, TypeError, ValueError) as err:
        raise PullErr(f"{records / PROVENANCE_FILE}: {err}") from err
    if provenance.workdir_id != workdir_id:
        raise PullErr(f"{records / PROVENANCE_FILE}: is for {provenance.workdir_id}")
    if provenance.fingerprint != fingerprint(config):
        raise PullErr(
            f"{workdir_id}: was seeded under a different setup (its state directory, store or "
            "checks); run this the way the pull was started, with the same environment"
        )
    return WorkDir(path, records, provenance)


def remove(config: UserConfig, workdir_id: str) -> None:
    """Remove the working directory and its records, if they're there."""
    root = pull_root(config)
    check_id(workdir_id)
    for path in (root / WORK_DIR / workdir_id, root / RECORDS_DIR / workdir_id):
        if os.path.lexists(path) and not os.path.islink(path):
            shutil.rmtree(path)


def _is_private_dir(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    return fileutil.is_windows() or (st.st_uid == os.getuid() and not stat.S_IMODE(st.st_mode) & 0o077)


def _write(directory: Path, name: str, data: jdata.DictJsonData) -> None:
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    fileutil.write_private(directory, [name], text.encode("ascii"))


def _read(path: Path) -> jdata.DictJsonData | None:
    try:
        fd = os.open(path, os.O_RDONLY | fileutil.O_NOFOLLOW | fileutil.O_BINARY)
    except FileNotFoundError:
        return None
    except OSError as err:
        raise PullErr(f"{path}: can't be read ({err})") from err
    with os.fdopen(fd, "rb") as inp:
        st = os.fstat(inp.fileno())
        if not fileutil.is_windows() and (st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077):
            raise PullErr(f"{path}: isn't private to you")
        raw = inp.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise PullErr(f"{path}: larger than {MAX_RECORD_BYTES} bytes")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as err:
        raise PullErr(f"{path}: not JSON ({err})") from err
    if not isinstance(value, dict):
        raise PullErr(f"{path}: not a record")
    return value
