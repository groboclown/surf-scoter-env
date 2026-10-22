#SPDX:MIT
"""Find the user's acceptance for a project, from the universal configuration file.

`verified_find.py` runs this for `scoter`, between the generator's second
run (with the project) and its third (with the accepted settings).

The project's files are snapshotted, and the setup values worked out, from
the universal configuration file; the environment's record gives the
newest usable acceptance that matches them.  A match on meaning (the files
differ only in comments and the like) is carried forward: a new acceptance
for these files, with the earlier one's settings and expiry.
"""
import datetime
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
    # The acceptance's `accepted.json`; None without a match.
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
    verified = config.optional_path("verification", "store")
    if project is None or verified is None or not verified.is_dir():
        return Lookup()
    project_dir = os.path.realpath(project)
    verified_store = store.VerifiedStore.open(verified)
    env_id = store.project_id(config, project_dir)
    record = records.load_environment(verified_store, project_dir, env_id)
    if record is None:
        return Lookup()

    current = project_snapshot(config, Path(project_dir))
    setup = records.SetupRecord(records.setup_values(
        config, vcs_section(config), verified_containers(config),
    ))
    found = record.find(current.manifest, setup, records.now_utc(), project_branch(config))
    if found is None:
        return Lookup()
    acceptance = found.acceptance
    notice = None
    if found.kind == records.MatchKind.EXEMPT:
        assert record.exemption is not None  # nosec: B101  # an exempt match has one
        notice = exempt_notice(config, record.exemption, acceptance.suggestion, current.manifest)
    if found.kind == records.MatchKind.MEANING:
        acceptance = _carry_forward(verified_store, project_dir, env_id, found, current, config)
        notice = (
            "The project's files changed only in comments or layout since you accepted them; "
            "the acceptance carries on, with the same settings and expiry."
        )
    accepted = verified_store.root / env_id / records.ACCEPTANCES_DIR / acceptance.id / ACCEPTED_FILE
    return Lookup(found.kind, acceptance.id, accepted, notice)


def project_branch(config: UserConfig) -> str | None:
    """The project's branch, as the generator gives it (`scoter:project-branch`); None without one."""
    branch = config.raw("active-config-locations", "project-branch")
    return branch if isinstance(branch, str) and branch else None


def is_settings_file(config: UserConfig, rel: str) -> bool:
    """Whether a snapshot's file is one whose accepted version an exemption keeps:
    the project file or a `dev-env.yaml` (settings, not a container's files)."""
    return (
        rel == config.optional_value("scoter", "project-file-name")
        or rel.rsplit("/", 1)[-1] == snapshot.DEV_ENV_FILE
    )


def settings_files(config: UserConfig, manifest: snapshot.Manifest) -> list[str]:
    """The files in a snapshot whose accepted version an exemption keeps."""
    return sorted(rel for rel in manifest.files if is_settings_file(config, rel))


def ignored_changes(
    config: UserConfig, accepted: snapshot.Manifest, current: snapshot.Manifest,
) -> list[str]:
    """The settings files an exemption keeps that differ now from their accepted version."""
    names = set(settings_files(config, accepted)) | set(settings_files(config, current))

    def sha(manifest: snapshot.Manifest, rel: str) -> str | None:
        entry = manifest.files.get(rel)
        return None if entry is None else entry.sha256

    return sorted(rel for rel in names if sha(accepted, rel) != sha(current, rel))


def exempt_notice(
    config: UserConfig, exemption: records.Exemption,
    accepted: snapshot.Manifest, current: snapshot.Manifest,
) -> str:
    """What a run under an exemption does with the project's files, in a line."""
    on = f" on {exemption.branch}" if exemption.branch else ""
    ignored = ignored_changes(config, accepted, current)
    text = (
        f"Exempt{on} until {exemption.until.isoformat()}: the run uses your container files "
        "as they are"
    )
    if ignored:
        text += (
            "; it ignores your changes to " + ", ".join(ignored)
            + " until you run `scoter verify --with-exemption`"
        )
    return text + "."


def no_match_reason(config: UserConfig, now: datetime.datetime | None = None) -> str:
    """Why no acceptance holds for the project, for the user: one sentence.

    Read only, unlike `find` (which carries an acceptance forward): never
    accepted; the acceptance of these files has expired; the setup changed
    since (which values); or the files changed since the newest acceptance
    (which files).
    """
    never = "You haven't accepted this project's settings."
    project = config.active_location("project-dir")
    verified = config.optional_path("verification", "store")
    if project is None or verified is None or not verified.is_dir():
        return never
    project_dir = os.path.realpath(project)
    verified_store = store.VerifiedStore.open(verified)
    record = records.load_environment(
        verified_store, project_dir, store.project_id(config, project_dir),
    )
    accepted = list(record.acceptances) if record else []
    if not accepted:
        return never
    now = now or records.now_utc()
    current = project_snapshot(config, Path(project_dir))
    setup = records.SetupRecord(records.setup_values(
        config, vcs_section(config), verified_containers(config),
    ))
    same_files = [
        a for a in accepted
        if a.suggestion.meaning_id == current.manifest.meaning_id
    ]
    same_setup = [a for a in same_files if a.setup.id == setup.id]
    if same_setup:
        newest = same_setup[0]
        if newest.validity(now) == records.Validity.EXPIRED:
            return f"Your acceptance of these files ({newest.id}) expired {newest.expires_at.isoformat()}."
        return f"Your acceptance of these files ({newest.id}) can't be used ({newest.validity(now)})."
    if same_files:
        changes = records.setup_changes(same_files[0].setup.values, setup.values)
        return (
            f"Your setup changed since you accepted these files ({same_files[0].id})"
            + (": " + ", ".join(changes) if changes else "") + "."
        )
    newest = accepted[0]
    changed = newest.suggestion.changed(current.manifest)
    return (
        f"The project's files changed since you last accepted them ({newest.id})"
        + (": " + ", ".join(changed) if changed else "") + "."
    )


def limits(config: UserConfig) -> snapshot.SnapshotLimits:
    """The snapshot limits, from the setup's `verification: max-*` values."""
    defaults = snapshot.SnapshotLimits()
    return snapshot.SnapshotLimits(
        max_file_size=config.optional_int_value("verification", "max-file-size")
        or defaults.max_file_size,
        max_snapshot_size=config.optional_int_value("verification", "max-snapshot-size")
        or defaults.max_snapshot_size,
    )


def container_dirs(config: UserConfig) -> list[Path]:
    """The container directories the dev container builds from."""
    parents = (
        [directory for _, directory in config.dev_container_parent_builds()]
        if config.has_dev_container() else []
    )
    return snapshot.container_dirs(config.active_location("dev-container-path"), parents)


def project_snapshot(config: UserConfig, project_dir: Path) -> snapshot.Snapshot:
    """The project's files, as an acceptance records them."""
    files_dir = config.optional_value("scoter", "project-files-dir")
    file_name = config.optional_value("scoter", "project-file-name")
    if files_dir is None or file_name is None:
        raise VerifiedStoreErr("the setup names no project files directory or project file")
    parsed = snapshot.ParsedFiles(project=config.project_setup(), dev_container=config.dev_container())
    return snapshot.snapshot_project(
        project_dir / files_dir, file_name, container_dirs(config), parsed, limits(config),
    )


def vcs_section(config: UserConfig) -> str | None:
    """The setup section for the project's VCS, if the setup has one."""
    vcs = config.project_str("vcs")
    if vcs is None:
        return None
    section = vcs + VCS_SECTION_SUFFIX
    return section if config.has_section(section) else None


def verified_roots(config: UserConfig) -> list[Path]:
    """The setup's `verification: verified-containers` directories, as real paths."""
    return [
        Path(os.path.realpath(p)) for p in config.optional_path_list("verification", "verified-containers")
    ]


def verified_container_snapshots(config: UserConfig) -> dict[str, snapshot.Snapshot]:
    """The build's container directories in `verification: verified-containers` (real paths), with their snapshots."""
    roots = verified_roots(config)
    found: dict[str, snapshot.Snapshot] = {}
    for directory in container_dirs(config):
        real = Path(os.path.realpath(directory))
        if any(real.is_relative_to(root) for root in roots):
            found[str(real)] = snapshot.snapshot_dir(real, limits(config))
    return found


def verified_containers(config: UserConfig) -> dict[str, str]:
    """The build's container directories in `verification: verified-containers`, with their manifests' ids."""
    return {d: s.manifest.id for d, s in verified_container_snapshots(config).items()}


def _carry_forward(
    verified_store: store.VerifiedStore,
    project_dir: str,
    env_id: str,
    found: records.Match,
    current: snapshot.Snapshot,
    config: UserConfig,
) -> records.Acceptance:
    """Record a new acceptance for `current`, from a match on meaning."""
    with verified_store.locked():
        # Again, under the lock: another run may have changed it.
        record = records.load_environment(verified_store, project_dir, env_id)
        if record is None:
            raise VerifiedStoreErr(f"{verified_store.root / env_id}: went away")
        manifest = current.store(verified_store)
        carried, replaced = record.carry_forward(found, manifest, records.now_utc(), found.acceptance.branch)
        source = (env_id, records.ACCEPTANCES_DIR, found.acceptance.id)
        for name in verified_store.list_dir(*source):
            data = verified_store.read_bytes(*source, name, max_size=store.MAX_JSON_BYTES)
            if data is not None:
                verified_store.write_bytes(
                    env_id, records.ACCEPTANCES_DIR, carried.id, name, data=data,
                )
        records.save_environment(verified_store, record)
        # After the copy: the acceptance carried from is among those replaced.
        records.remove_replaced(verified_store, record, replaced)
    return carried
