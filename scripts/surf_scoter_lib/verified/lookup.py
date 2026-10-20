#SPDX:MIT
"""Find the user's acceptance for a project, from the universal configuration file.

`verified_find.py` runs this for `scoter`, between the generator's second
run (with the project) and its third (with the accepted settings).  See
docs/contributors/projects-v1.md, step 8.

The project's files are snapshotted, and the setup values worked out, from
the universal configuration file; the environment's record gives the
newest usable acceptance that matches them.  A match on meaning (the files
differ only in comments and the like) is carried forward: a new acceptance
for these files, with the earlier one's settings and expiry.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from .. import jdata
from ..err import VerifiedStoreErr
from . import records, snapshot, store

ACCEPTED_FILE = "accepted.json"
VCS_SECTION_SUFFIX = "-vcs"


@dataclass(frozen=True)
class Lookup:
    """What the lookup found."""

    match: records.MatchKind | None = None
    acceptance: str | None = None
    # The acceptance's `accepted.json`; None for a blanket acceptance, or no match.
    accepted_settings: Path | None = None
    notice: str | None = None

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data, for `scoter`."""
        return {
            "match": None if self.match is None else str(self.match),
            "acceptance": self.acceptance,
            "accepted-settings": None if self.accepted_settings is None else str(self.accepted_settings),
            "notice": self.notice,
        }


def _get(data: jdata.JsonData, *keys: str) -> jdata.JsonData:
    """`data[k1][k2]...`, or None where any of it is missing."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _text(data: jdata.JsonData, *keys: str) -> str | None:
    value = _get(data, *keys)
    return value if isinstance(value, str) else None


def _int(data: jdata.JsonData, *keys: str, default: int) -> int:
    value = _get(data, *keys)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def find(suc: jdata.DictJsonData) -> Lookup:
    """The acceptance for the project in `suc`, the universal configuration file's data."""
    project = _text(suc, "active-config-locations", "project-dir")
    verified = _text(suc, "resolved", "scoter", "paths", "verified-dir")
    if project is None or verified is None or not os.path.isdir(verified):
        return Lookup()
    project_dir = os.path.realpath(project)
    verified_store = store.VerifiedStore.open(Path(verified))
    env_id = store.environment_id(Path(project_dir))
    record = records.load_environment(verified_store, project_dir, env_id)
    if record is None:
        return Lookup()

    current = _snapshot(suc, Path(project_dir))
    setup = records.SetupRecord(records.setup_values(
        suc, _vcs_section(suc), _verified_containers(suc),
    ))
    now = records.now_utc()
    found = record.find(current.manifest, setup, now)
    if found is None:
        return Lookup()
    acceptance = found.acceptance
    notice = None
    if found.kind == records.MatchKind.MEANING:
        acceptance = _carry_forward(verified_store, project_dir, env_id, found, current, suc)
        notice = (
            "The project's files changed only in comments or layout since you accepted them; "
            "the acceptance carries on, with the same settings and expiry."
        )
    accepted = None
    if acceptance.kind != records.Kind.BLANKET:
        accepted = verified_store.root / env_id / records.ACCEPTANCES_DIR / acceptance.id / ACCEPTED_FILE
    return Lookup(found.kind, acceptance.id, accepted, notice)


def _limits(suc: jdata.DictJsonData) -> snapshot.SnapshotLimits:
    defaults = snapshot.SnapshotLimits()
    return snapshot.SnapshotLimits(
        max_file_size=_int(
            suc, "resolved", "scoter", "values", "verified-max-file-size",
            default=defaults.max_file_size,
        ),
        max_snapshot_size=_int(
            suc, "resolved", "scoter", "values", "verified-max-snapshot-size",
            default=defaults.max_snapshot_size,
        ),
    )


def _container_dirs(suc: jdata.DictJsonData) -> list[Path]:
    dev_container = _text(suc, "active-config-locations", "dev-container-path")
    builds = _get(suc, "dev-container", "parent-builds")
    parents = [
        Path(build["dir"])
        for build in (builds if isinstance(builds, list) else [])
        if isinstance(build, dict) and isinstance(build.get("dir"), str)
    ]
    return snapshot.container_dirs(Path(dev_container) if dev_container else None, parents)


def _snapshot(suc: jdata.DictJsonData, project_dir: Path) -> snapshot.Snapshot:
    files_dir = _text(suc, "resolved", "scoter", "values", "project-files-dir")
    file_name = _text(suc, "resolved", "scoter", "values", "project-file-name")
    if files_dir is None or file_name is None:
        raise VerifiedStoreErr("the setup names no project files directory or project file")
    parsed = snapshot.ParsedFiles(
        project=_get(suc, "current-project-setup"),
        dev_container=_get(suc, "dev-container"),
    )
    return snapshot.snapshot_project(
        project_dir / files_dir, file_name, _container_dirs(suc), parsed, _limits(suc),
    )


def _vcs_section(suc: jdata.DictJsonData) -> str | None:
    vcs = _text(suc, "current-project-setup", "vcs")
    if vcs is None:
        return None
    section = vcs + VCS_SECTION_SUFFIX
    return section if isinstance(_get(suc, "resolved", section), dict) else None


def _verified_containers(suc: jdata.DictJsonData) -> dict[str, str]:
    """The build's container directories in `dev-containers-verified`, with their manifests' ids."""
    listed = _get(suc, "resolved", "scoter", "path-lists", "dev-containers-verified")
    roots = [Path(os.path.realpath(p)) for p in (listed if isinstance(listed, list) else [])
             if isinstance(p, str)]
    found: dict[str, str] = {}
    for directory in _container_dirs(suc):
        real = Path(os.path.realpath(directory))
        if any(real.is_relative_to(root) for root in roots):
            found[str(real)] = snapshot.snapshot_dir(real, _limits(suc)).manifest.id
    return found


def _carry_forward(
    verified_store: store.VerifiedStore,
    project_dir: str,
    env_id: str,
    found: records.Match,
    current: snapshot.Snapshot,
    suc: jdata.DictJsonData,
) -> records.Acceptance:
    """Record a new acceptance for `current`, from a match on meaning."""
    keep = _int(suc, "resolved", "scoter", "values", "verified-keep", default=10)
    with verified_store.locked():
        # Again, under the lock: another run may have changed it.
        record = records.load_environment(verified_store, project_dir, env_id)
        if record is None:
            raise VerifiedStoreErr(f"{verified_store.root / env_id}: went away")
        manifest = current.store(verified_store)
        carried = record.carry_forward(found, manifest, records.now_utc(), found.acceptance.branch)
        source = (env_id, records.ACCEPTANCES_DIR, found.acceptance.id)
        for name in verified_store.list_dir(*source):
            data = verified_store.read_bytes(*source, name, max_size=store.MAX_JSON_SIZE)
            if data is not None:
                verified_store.write_bytes(
                    env_id, records.ACCEPTANCES_DIR, carried.id, name, data=data,
                )
        records.save_environment(verified_store, record, keep)
    return carried
