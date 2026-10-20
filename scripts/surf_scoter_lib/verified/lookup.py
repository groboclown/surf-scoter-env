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
from ..user_config import UserConfig
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


def find(config: UserConfig) -> Lookup:
    """The acceptance for the project in the universal configuration file."""
    project = config.active_location("project-dir")
    verified = config.optional_path("scoter", "verified-dir")
    if project is None or verified is None or not verified.is_dir():
        return Lookup()
    project_dir = os.path.realpath(project)
    verified_store = store.VerifiedStore.open(verified)
    env_id = store.environment_id(Path(project_dir))
    record = records.load_environment(verified_store, project_dir, env_id)
    if record is None:
        return Lookup()

    current = _snapshot(config, Path(project_dir))
    setup = records.SetupRecord(records.setup_values(
        config, _vcs_section(config), _verified_containers(config),
    ))
    found = record.find(current.manifest, setup, records.now_utc())
    if found is None:
        return Lookup()
    acceptance = found.acceptance
    notice = None
    if found.kind == records.MatchKind.MEANING:
        acceptance = _carry_forward(verified_store, project_dir, env_id, found, current, config)
        notice = (
            "The project's files changed only in comments or layout since you accepted them; "
            "the acceptance carries on, with the same settings and expiry."
        )
    accepted = None
    if acceptance.kind != records.Kind.BLANKET:
        accepted = verified_store.root / env_id / records.ACCEPTANCES_DIR / acceptance.id / ACCEPTED_FILE
    return Lookup(found.kind, acceptance.id, accepted, notice)


def _limits(config: UserConfig) -> snapshot.SnapshotLimits:
    defaults = snapshot.SnapshotLimits()
    return snapshot.SnapshotLimits(
        max_file_size=config.optional_int_value("scoter", "verified-max-file-size")
        or defaults.max_file_size,
        max_snapshot_size=config.optional_int_value("scoter", "verified-max-snapshot-size")
        or defaults.max_snapshot_size,
    )


def _container_dirs(config: UserConfig) -> list[Path]:
    parents = (
        [directory for _, directory in config.dev_container_parent_builds()]
        if config.has_dev_container() else []
    )
    return snapshot.container_dirs(config.active_location("dev-container-path"), parents)


def _snapshot(config: UserConfig, project_dir: Path) -> snapshot.Snapshot:
    files_dir = config.optional_value("scoter", "project-files-dir")
    file_name = config.optional_value("scoter", "project-file-name")
    if files_dir is None or file_name is None:
        raise VerifiedStoreErr("the setup names no project files directory or project file")
    parsed = snapshot.ParsedFiles(project=config.project_setup(), dev_container=config.dev_container())
    return snapshot.snapshot_project(
        project_dir / files_dir, file_name, _container_dirs(config), parsed, _limits(config),
    )


def _vcs_section(config: UserConfig) -> str | None:
    vcs = config.project_str("vcs")
    if vcs is None:
        return None
    section = vcs + VCS_SECTION_SUFFIX
    return section if config.has_section(section) else None


def _verified_containers(config: UserConfig) -> dict[str, str]:
    """The build's container directories in `dev-containers-verified`, with their manifests' ids."""
    roots = [
        Path(os.path.realpath(p)) for p in config.optional_path_list("scoter", "dev-containers-verified")
    ]
    found: dict[str, str] = {}
    for directory in _container_dirs(config):
        real = Path(os.path.realpath(directory))
        if any(real.is_relative_to(root) for root in roots):
            found[str(real)] = snapshot.snapshot_dir(real, _limits(config)).manifest.id
    return found


def _carry_forward(
    verified_store: store.VerifiedStore,
    project_dir: str,
    env_id: str,
    found: records.Match,
    current: snapshot.Snapshot,
    config: UserConfig,
) -> records.Acceptance:
    """Record a new acceptance for `current`, from a match on meaning."""
    keep = config.optional_int_value("scoter", "verified-keep") or 10
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
