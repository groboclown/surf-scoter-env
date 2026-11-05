#SPDX:MIT
"""`scoter pull seed`: copy the project's files into a new working directory.

Seeding is the only phase that reads the project, so every defensive rule
is here (VER-23):

1. Snapshot the project's whole `.surf-scoter.d`: as committed at a ref
   (`vcs_files.snapshot_commit`), by default, or as it is on disk
   (`snapshot.snapshot_source`).  Either refuses links and special files,
   odd names and anything over the limits, before anything parses it.
2. Restore the snapshot into a private staging directory, and run the
   generator on that copy (`--config-dir`), never on the live files.  What
   the build uses is selected from the same bytes (`select_build`, VER-11).
3. Restore only those files into a new working directory's `files/`.
4. Ask the VCS about the copied files (`vcs_files.file_status`).
5. Resolve the host commands through the path checks, keeping the
   developer's own entries, and write the host file beside `files/`.
6. Record the provenance, outside the working directory (VER-25).

The seeded files' blobs go into the store, so the base can be recorded at
approval.
"""
import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import fileutil, host_file, jdata, vcs_files
from ..err import ConfigErr
from ..user_config import FoundProject, UserConfig
from ..verified import configs, lookup, snapshot, store
from ..verified.store import now_utc
from . import unlocked, workdir

Generate = Callable[[UserConfig, list[str], Path], tuple[jdata.DictJsonData | None, list[str]]]
DEFAULT_REF = "HEAD"
# Where seeding reads from: the project, the unlocked copy (`scoter lock`),
# or the configuration in use (`scoter review`).
FROM_PROJECT = "project"
FROM_UNLOCKED = "unlocked"
FROM_LOCKED = "locked"
SOURCES = (FROM_PROJECT, FROM_UNLOCKED, FROM_LOCKED)


@dataclass(frozen=True)
class Seeded:
    """A new working directory."""

    workdir_id: str
    path: Path
    # The seeded files and the setup's checks are as at the last pull.
    unchanged: bool
    warnings: list[str]

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data, as `scoter pull seed` prints it."""
        return {
            "id": self.workdir_id,
            "path": str(self.path),
            "unchanged": self.unchanged,
            "warnings": list(self.warnings),
        }


def require_found(config: UserConfig, *, start_ok: bool = False) -> FoundProject:
    """The project `scoter` found; a `ConfigErr` without one.

    By its files, or its link in the store; with `start_ok`, the directory
    the search started from will do (`init` and `unlock --from-template`).
    """
    found = config.found_project()
    if found is None or (found.found_by == "start" and not start_ok):
        raise ConfigErr(
            "No project was found here: run this in a project, or give --project-dir.  "
            "`scoter init` starts one."
        )
    return found


def files_location(config: UserConfig, found: FoundProject) -> tuple[Path, str]:
    """The project's files directory, and the project file's name in it."""
    files_dir = config.optional_value("scoter", "project-files-dir")
    file_name = config.optional_value("scoter", "project-file-name")
    if files_dir is None or file_name is None:
        raise ConfigErr("the setup names no `scoter: values: project-files-dir` or `project-file-name`")
    return Path(os.path.realpath(found.project_dir)) / files_dir, file_name


def open_store(config: UserConfig) -> store.VerifiedStore:
    """The verified store, made if it isn't there yet."""
    root = config.optional_path("verification", "store")
    if root is None:
        raise ConfigErr("the setup names no `verification: paths: store`")
    return store.VerifiedStore.open(root, if_missing=store.IfMissing.CREATE)


def generate_on(
    config: UserConfig, found: FoundProject, staged: Path, work: Path, generate: Generate,
) -> UserConfig:
    """The generator's view of the staged copy `staged`, for the found project."""
    work.mkdir(mode=fileutil.MODE_USER_RWX)
    data, problems = generate(config, [*found.generator_args(), "--config-dir", str(staged)], work)
    if data is None:
        raise workdir.PullErr("The project's files can't be read: " + "; ".join(problems))
    return UserConfig(data)


def stage(source: snapshot.Snapshot, dest: Path, limits: snapshot.SnapshotLimits) -> Path:
    """Write `source`'s files into `dest/staged`, a new private directory; it."""
    staged = dest / "staged"
    staged.mkdir(mode=fileutil.MODE_USER_RWX)
    snapshot.write_files(source.manifest, staged, limits, lambda rel, _: source.contents[rel])
    return staged


def check_vcs(staged: UserConfig, found: FoundProject) -> None:
    """The project's `vcs` must be the repository's it was found in, as the run checks."""
    vcs = staged.project_str("vcs")
    if vcs is None or not staged.has_run(vcs + "-vcs", "find-root"):
        return
    if found.vcs != vcs:
        where = f"a {found.vcs} repository" if found.vcs else "no repository"
        raise workdir.PullErr(f"The project's `vcs` is {vcs!a}, but it's in {where}.")


def differences(before: snapshot.Manifest, after: snapshot.Manifest) -> list[str]:
    """The paths whose contents or executable bit differ, exactly (not on meaning)."""
    def exact(entry: snapshot.FileEntry | None) -> tuple[str, bool] | None:
        return None if entry is None else (entry.sha256, entry.executable)
    paths = set(before.files) | set(after.files)
    return sorted(p for p in paths if exact(before.files.get(p)) != exact(after.files.get(p)))


@dataclass
class Source:
    """What seeding reads, and what it's compared with."""

    snapshot: snapshot.Snapshot
    record: jdata.DictJsonData
    # The host file whose `user` entries the new one keeps.
    previous_host: jdata.DictJsonData | None
    # What the seeded files are compared with: the base for the project, the
    # configuration unlocked from for the unlocked copy.
    compare_with: snapshot.Manifest | None


def read_source(
    config: UserConfig,
    found: FoundProject,
    verified: store.VerifiedStore,
    link: configs.Link | None,
    source: str,
    *,
    ref: str,
    working_tree: bool,
    warnings: list[str],
) -> Source:
    """The source's whole snapshot, within the limits, before anything parses it."""
    files_dir, file_name = files_location(config, found)
    limits = lookup.limits(config)
    if source == FROM_UNLOCKED:
        if link is None or link.unlocked is None:
            raise workdir.PullErr("Your configuration isn't unlocked: `scoter unlock` makes a copy to edit.")
        from_config = None if link.unlocked.from_id is None else configs.load_config(verified, link.id, link.unlocked.from_id)
        taken, host = unlocked.read(unlocked.root(config, link.id), file_name, limits)
        return Source(
            taken, {"kind": FROM_UNLOCKED, "from": link.unlocked.from_id, "template": link.unlocked.template},
            host if host is not None else (from_config.host_file if from_config else None),
            None if from_config is None else from_config.snapshot,
        )
    if source == FROM_LOCKED:
        if link is None or link.in_use is None:
            raise workdir.PullErr("No configuration is in use to review: `scoter pull` makes one.")
        in_use = configs.load_config(verified, link.id, link.in_use)
        if in_use is None:
            raise workdir.PullErr(f"Your configuration in use ({link.in_use}) isn't in the store any more.")
        return Source(
            snapshot.from_store(verified, in_use.snapshot),
            {"kind": FROM_LOCKED, "config": in_use.id, "name": in_use.name},
            in_use.host_file, None,
        )
    # From the project: committed at `ref`, by default.
    previous = _host_file_in_use(verified, link)
    base = None if link is None else link.base
    can_show = found.vcs is not None and found.vcs_root is not None and vcs_files.can_show(config, found.vcs)
    if working_tree or not can_show:
        if not working_tree:
            warnings.append(
                "The project's VCS can't give its committed files, so they're read from the "
                "working tree: changes nobody committed are pulled too."
            )
        taken = snapshot.snapshot_source(files_dir, file_name, limits)
        record: jdata.DictJsonData = {"kind": FROM_PROJECT, "from": "working-tree", "ref": None, "commit": None}
        return Source(taken, record, previous, base)
    assert found.vcs is not None and found.vcs_root is not None  # nosec: B101  # can_show
    commit, taken = vcs_files.snapshot_commit(config, found.vcs, found.vcs_root, ref, files_dir, limits)
    return Source(taken, {"kind": FROM_PROJECT, "from": "commit", "ref": ref, "commit": commit}, previous, base)


def seed(
    config: UserConfig,
    *,
    generate: Generate,
    source: str = FROM_PROJECT,
    ref: str = DEFAULT_REF,
    working_tree: bool = False,
    path_env: str | None = None,
) -> Seeded:
    """Seed a new working directory from `source`; it.

    From the project: the files committed at `ref`, or with `working_tree`,
    the files on disk; a VCS that can't give committed files is read from
    the working tree, with a warning.  From the unlocked copy, or the
    configuration in use: as they are.
    """
    if source not in SOURCES:
        raise workdir.PullErr(f"{source!a} isn't a source to seed from")
    found = require_found(config)
    files_dir, file_name = files_location(config, found)
    limits = lookup.limits(config)
    verified = open_store(config)
    project_dir = os.path.realpath(found.project_dir)
    env_id = store.project_id(config, project_dir)
    warnings: list[str] = []
    link = configs.load_link(verified, project_dir, env_id)

    # 1. The whole source, within the limits, before anything parses it.
    read = read_source(
        config, found, verified, link, source, ref=ref, working_tree=working_tree, warnings=warnings,
    )
    warnings.extend(read.snapshot.warnings)
    from_project = source == FROM_PROJECT
    workdir_id, path, records = workdir.create(config)
    try:
        # 2. What the build uses, from the copy the generator read.
        with tempfile.TemporaryDirectory(dir=workdir.pull_root(config)) as tmp:
            staged = stage(read.snapshot, Path(tmp), limits)
            staged_config = generate_on(config, found, staged, Path(tmp) / "generator", generate)
            check_vcs(staged_config, found)
            parsed = snapshot.ParsedFiles(staged_config.project_setup(), staged_config.dev_container())
            selected = snapshot.select_build(
                read.snapshot, staged, file_name, lookup.container_dirs(staged_config), parsed,
            )
            # 5. The host commands, through the path checks (VER-31).
            resolution = host_file.resolve(staged_config, path_env, read.previous_host)
        if not from_project:
            # A program found again at the same name may not be the one approved (BRIDGE-13).
            warnings.extend(f"Host commands, resolved again: {c}" for c in resolution.changes)
        # 3. Only the build's files, into the working directory.
        snapshot.write_files(
            selected.manifest, path / workdir.FILES_DIR, limits, lambda rel, _: selected.contents[rel],
        )
        host_file.write(path, resolution.contents)
        with verified.locked():
            selected.store(verified)
        # 4. The VCS, about the copied files.
        vcs_report = (
            _vcs_report(config, found, read.record, files_dir, path / workdir.FILES_DIR) if from_project
            else _unknown(path / workdir.FILES_DIR)
        )
        # 6. The provenance, beside the working directory.
        since_base = None if read.compare_with is None else differences(read.compare_with, selected.manifest)
        fingerprint = workdir.fingerprint(config)
        recorded = jdata.find_str(link.provenance, "fingerprint") if link and link.provenance else None
        unchanged = from_project and since_base == [] and recorded == fingerprint
        workdir.save_provenance(records, workdir.Provenance(
            workdir_id=workdir_id,
            env_id=env_id,
            project_dir=project_dir,
            source=read.record,
            seeded=selected.manifest,
            vcs=vcs_report.to_json(),
            since_base=since_base,
            unchanged=unchanged,
            fingerprint=fingerprint,
            created_at=now_utc(),
            warnings=warnings,
            source_id=read.snapshot.manifest.id if from_project else None,
        ))
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        shutil.rmtree(records, ignore_errors=True)
        raise
    return Seeded(workdir_id, path, unchanged, warnings)


def _host_file_in_use(verified: store.VerifiedStore, link: configs.Link | None) -> jdata.DictJsonData | None:
    """The host file of the configuration in use, which the new one keeps the developer's entries of."""
    if link is None or link.in_use is None:
        return None
    config = configs.load_config(verified, link.id, link.in_use)
    return None if config is None else config.host_file


def _unknown(copy: Path) -> vcs_files.StatusReport:
    """A VCS report that knows nothing: every copied file `unknown`."""
    present: Sequence[str] = sorted(
        Path(d, n).relative_to(copy).as_posix() for d, _, names in os.walk(copy) for n in names
    )
    return vcs_files.StatusReport(
        None, {rel: vcs_files.FileStatus(vcs_files.Status.UNKNOWN, None) for rel in present},
    )


def _vcs_report(
    config: UserConfig, found: FoundProject, source: jdata.DictJsonData, files_dir: Path, copy: Path,
) -> vcs_files.StatusReport:
    """The VCS's report on the copied files: against the commit pulled, or the ref."""
    if found.vcs is None or found.vcs_root is None:
        return _unknown(copy)
    against = jdata.find_str(source, "commit") or jdata.find_str(source, "ref") or DEFAULT_REF
    return vcs_files.file_status(config, found.vcs, found.vcs_root, against, files_dir, copy)
