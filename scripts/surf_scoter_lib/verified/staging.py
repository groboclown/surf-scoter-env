#SPDX:MIT
"""Stage the accepted copy of a project's files, for the run to build from.

`main_run.py` builds the dev container from the files the user accepted,
restored from the verified store, never from the live `.surf-scoter.d`,
which can change between the check and the build.

* `find_accepted` finds the acceptance whose `accepted.json` the generator
  loaded, from the environment's record, not by looking at the live files:
  with `dev-container: use: {name}`, the universal file's dev container is
  the user's, not the one the snapshot was taken of.
* `stage` restores its snapshot, laid out like a project.
* `build_plan` checks that the generator's view of the copy is what the
  universal file holds, and says what to build from.
* Under an exemption (`exempt.py`), when the run's acceptance is
  the exemption's, `adopt_accepted` first gives the run the accepted
  project file's and `dev-env.yaml` files' settings in place of the live
  ones, and `stage` lays the live container files over the accepted copy:
  they're used as they are, once the setup's `project-files` checks pass.
* `rewrite_images` applies the acceptance's `images` to a staged Dockerfile.
"""
import datetime
import os
import re
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from .. import checks, fileutil, jdata
from ..err import VerifiedStoreErr
from ..user_config import CURRENT_PROJECT_SETUP, UserConfig
from . import accept, lookup, records, snapshot, store

STAGED_PROJECT = "project"
DOCKERFILE = "Dockerfile"

_FROM = re.compile(r"FROM\s", re.IGNORECASE)
_STAGE = re.compile(r"\s+AS\s+(\S+)\s*$", re.IGNORECASE)


class StagingErr(VerifiedStoreErr):
    """The accepted copy can't be built from: it's missing, expired or doesn't match.

    `reverify` says whether a new review would help (the acceptance has
    expired, say), rather than running again (the files changed meanwhile).
    """

    def __init__(self, message: str, *, reverify: bool = True) -> None:
        super().__init__(message)
        self.reverify = reverify


@dataclass(frozen=True)
class Accepted:
    """The acceptance the run's accepted settings came from."""

    store: store.VerifiedStore
    acceptance: records.Acceptance
    manifest: snapshot.Manifest
    env_id: str = ""
    # The exemption the run is under, when the acceptance is its own.
    exemption: records.Exemption | None = None


@dataclass(frozen=True)
class BuildPlan:
    """What to build the dev container from."""

    dev_container: Path
    # The local parents, lowest first: (tag, directory).
    parents: list[tuple[str, Path]]


def find_accepted(config: UserConfig, now: datetime.datetime) -> Accepted | None:
    """The acceptance whose `accepted.json` the generator loaded; None without one."""
    file = config.accepted_settings_file()
    if file is None:
        return None
    project = config.active_location("project-dir")
    verified = config.optional_path("verification", "store")
    if project is None or verified is None:
        raise StagingErr("accepted settings, but no project or `verification: paths: store` to find them in")
    project_dir = os.path.realpath(project)
    env_id = store.project_id(config, project_dir)
    verified_store = store.VerifiedStore.open(verified)
    real = Path(os.path.realpath(file))
    acceptances = Path(os.path.realpath(verified_store.root / env_id / records.ACCEPTANCES_DIR))
    if real.name != lookup.ACCEPTED_FILE or real.parent.parent != acceptances:
        raise StagingErr(f"{file}: isn't an acceptance in your verified store for this project")
    record = records.load_environment(verified_store, project_dir, env_id)
    found = None if record is None else next(
        (a for a in record.acceptances if a.id == real.parent.name), None,
    )
    if found is None:
        raise StagingErr(f"{file}: its acceptance isn't in your verified store any more")
    if not found.usable(now):
        raise StagingErr(f"your acceptance of this project's settings ({found.id}) has expired")
    assert record is not None  # nosec: B101  # it holds `found`
    exempt = record.exempt_acceptance(lookup.project_branch(config), now)
    exemption = record.exemption if exempt is not None and exempt.id == found.id else None
    return Accepted(verified_store, found, found.suggestion, env_id, exemption)


def stage(config: UserConfig, accepted: Accepted, dest: Path) -> Path:
    """Restore the accepted files under `dest` (empty, private), laid out like a project.

    Returns the staged project directory: `dest/project`, holding the
    project files directory.  Under an exemption (`accepted.exemption`),
    and once `adopt_accepted` has run, the container files are the live
    ones, in the containers the accepted settings name; the project file
    and the `dev-env.yaml` files are as accepted.
    """
    files_dir = _files_dir(config)
    project = dest / STAGED_PROJECT
    project.mkdir(mode=fileutil.MODE_USER_RWX)
    target = project / files_dir
    target.mkdir(mode=fileutil.MODE_USER_RWX)
    limits = lookup.limits(config)
    if accepted.exemption is None or config.accepted_container_name() is not None:
        snapshot.restore(accepted.store, accepted.manifest, target, limits)
        return project
    live = _live_files(config, accepted)
    kept = {
        rel: entry for rel, entry in accepted.manifest.files.items() if lookup.is_settings_file(config, rel)
    }
    files = {
        **{rel: entry for rel, entry in live.manifest.files.items() if not lookup.is_settings_file(config, rel)},
        **kept,
    }

    def read(rel: str, entry: snapshot.FileEntry) -> bytes:
        if rel in kept:
            return accepted.store.get_blob(entry.sha256, entry.size)
        return live.contents[rel]

    snapshot.write_files(snapshot.Manifest(files), target, limits, read)
    return project


def _live_files(config: UserConfig, accepted: Accepted) -> snapshot.Snapshot:
    """The live files of the containers the run's settings name (the accepted ones, adopted)."""
    files_dir = _files_dir(config)
    file_name = config.optional_value("scoter", "project-file-name")
    if file_name is None:
        raise StagingErr("the setup names no `scoter: values: project-file-name`")
    project = Path(os.path.realpath(config.require_active_location("project-dir")))
    try:
        return snapshot.snapshot_project(
            project / files_dir, file_name, lookup.container_dirs(config),
            snapshot.ParsedFiles(), lookup.limits(config),
        )
    except VerifiedStoreErr as err:
        raise StagingErr(
            f"under your exemption ({accepted.acceptance.id}), the container files can't be used: {err}",
            reverify=False,
        ) from err


def adopt_accepted(
    config: UserConfig,
    dest: Path,
    generate: Callable[[UserConfig, list[str], Path], tuple[jdata.DictJsonData | None, list[str]]],
    now: datetime.datetime,
) -> bool:
    """Under an exemption, have the run use the accepted project file and `dev-env.yaml` files.

    The generator read the run's `current-project-setup` and `dev-container`
    from the live files, which an exemption lets change; only their accepted
    versions count.  This replaces them in `config` with the accepted
    copy's, its paths as the live project's, so the sshd port, the user,
    the mounts and the build use what was accepted.  It runs before the
    run reads any of them.  False, with `config` as it was, when the run's
    acceptance isn't its exemption's.  `dest`: empty, private.
    """
    accepted = find_accepted(config, now)
    if accepted is None or accepted.exemption is None:
        return False
    staged_project = stage(config, replace(accepted, exemption=None), dest)
    staged = _generate(config, staged_project, dest, generate)
    project_setup = staged.project_setup()
    if project_setup is None:
        raise StagingErr("The accepted copy of the project's files has no project file.")
    if config.accepted_container_name() is not None:
        # The dev container is the user's own, read from where it is.
        config.use_accepted_files(project_setup, None)
        return True
    dev = staged.dev_container()
    if dev is None:
        raise StagingErr("The accepted copy of the project's files has no dev container.")
    live = _to_live(config, staged_project)
    config.use_accepted_files(project_setup, {
        **dev,
        "parent-builds": [{"dir": live(d), "tag": tag} for tag, d in staged.dev_container_parent_builds()],
    })
    config.set_active_location(
        "dev-container-path", Path(live(staged.require_active_location("dev-container-path"))),
    )
    return True


def _generate(
    config: UserConfig,
    staged_project: Path,
    dest: Path,
    generate: Callable[[UserConfig, list[str], Path], tuple[jdata.DictJsonData | None, list[str]]],
) -> UserConfig:
    work = dest / "generator"
    work.mkdir(mode=fileutil.MODE_USER_RWX)
    staged, problems = generate(config, ["--project-dir", str(staged_project)], work)
    if staged is None:
        raise StagingErr(
            "The accepted copy of the project's files can't be read: " + "; ".join(problems)
        )
    return UserConfig(staged)


def _to_live(config: UserConfig, staged_project: Path) -> Callable[[Path], str]:
    """A path in the staged copy, as the live project's; others, as they are."""
    files_dir = _files_dir(config)
    live_files = Path(os.path.realpath(config.require_active_location("project-dir"))) / files_dir
    staged_files = Path(os.path.realpath(staged_project)) / files_dir

    def live(path: Path) -> str:
        real = Path(os.path.realpath(path))
        if real.is_relative_to(staged_files):
            return str(live_files / real.relative_to(staged_files))
        return str(real)

    return live


def build_plan(config: UserConfig, staged: UserConfig, staged_project: Path) -> BuildPlan:
    """Check the generator's view of the staged copy (`staged`) against the run's, and plan the build.

    The run's `current-project-setup` and `dev-container` were parsed from
    the live files, before the lookup read them; the copy must parse to the
    same, or the files changed in between.  The project's container, and its
    parents inside the project files directory, build from the copy.  The
    user's own container (`use: {name}`) builds from where it is: it's
    outside the project, and trusted, like the setup.
    """
    if staged.raw(CURRENT_PROJECT_SETUP) != config.raw(CURRENT_PROJECT_SETUP):
        raise StagingErr(_CHANGED.format(what="project file"), reverify=False)
    if config.accepted_container_name() is not None:
        return BuildPlan(
            config.require_active_location("dev-container-path"),
            config.dev_container_parent_builds(),
        )
    live = _to_live(config, staged_project)
    dev_path = staged.require_active_location("dev-container-path")
    staged_dev = staged.dev_container()
    run_dev = config.dev_container()
    if staged_dev is None or run_dev is None:
        raise StagingErr(_CHANGED.format(what="dev container"), reverify=False)
    parents = staged.dev_container_parent_builds()
    mapped: jdata.DictJsonData = {
        **staged_dev,
        "parent-builds": [{"dir": live(d), "tag": tag} for tag, d in parents],
    }
    run_parents = [
        {"dir": str(Path(os.path.realpath(d))), "tag": tag}
        for tag, d in config.dev_container_parent_builds()
    ]
    if staged_dev.get("sshd-port") is None:
        # The files leave the port to the run, which chose it before staging.
        mapped.pop("sshd-port", None)
        run_dev = {k: v for k, v in run_dev.items() if k != "sshd-port"}
    if (
        live(dev_path) != os.path.realpath(config.require_active_location("dev-container-path"))
        or mapped != {**run_dev, "parent-builds": run_parents}
    ):
        raise StagingErr(_CHANGED.format(what="dev container"), reverify=False)
    return BuildPlan(dev_path, parents)


def stage_containers(
    config: UserConfig, accepted: Accepted, plan: BuildPlan, dest: Path,
) -> BuildPlan:
    """The build's other containers, staged in `dest` where they need to be.

    * Each `verification: verified-containers` container the build uses is restored
      from the copy kept with the acceptance (`accept.CONTAINERS_FILE`), so
      it's built as it was accepted, not as it is now.
    * The user's own container (`use: {name}`), trusted as their setup is,
      builds from where it is, unless the acceptance's `images` change a
      `FROM`: then from a copy, which they're applied to.
    """
    roots = lookup.verified_roots(config)
    stored: dict[str, snapshot.Manifest] | None = None
    count = 0

    def place(directory: Path) -> Path:
        nonlocal stored, count
        real = Path(os.path.realpath(directory))
        if not any(real.is_relative_to(root) for root in roots):
            return directory
        if stored is None:
            data = accepted.store.read_json(
                accepted.env_id, records.ACCEPTANCES_DIR, accepted.acceptance.id, accept.CONTAINERS_FILE,
                schema=accept.CONTAINERS_SCHEMA,
            )
            if data is None:
                raise StagingErr(
                    f"your acceptance ({accepted.acceptance.id}) was made before the containers in "
                    "`verification: verified-containers` were kept with it"
                )
            stored = {
                str(key[-1]): snapshot.Manifest.from_json(jdata.req_dict(data, *key))
                for key in jdata.find_dict_keys(data, "containers")
            }
        manifest = stored.get(str(real))
        if manifest is None:
            raise StagingErr(f"{real}: isn't among the containers your acceptance kept")
        count += 1
        target = dest / "containers" / str(count)
        target.mkdir(mode=fileutil.MODE_USER_RWX, parents=True)
        snapshot.restore(accepted.store, manifest, target, lookup.limits(config))
        return target

    parents = [(tag, place(directory)) for tag, directory in plan.parents]
    dev_container = place(plan.dev_container)
    images = {k: v for k, v in config.accepted_images().items() if k != v}
    if config.accepted_container_name() is not None and dev_container == plan.dev_container and images:
        copy = dest / "user-container"
        shutil.copytree(plan.dev_container, copy, symlinks=True)
        dev_container = copy
    return BuildPlan(dev_container, parents)


def apply_images(plan: BuildPlan, staged_root: Path, images: Mapping[str, str]) -> list[Path]:
    """Rewrite the staged Dockerfiles' `FROM`s with `images`; the Dockerfiles changed.

    Only the staged copies (under `staged_root`) are rewritten: a directory
    built from where it is is left as it is.
    """
    staged = Path(os.path.realpath(staged_root))
    local = [tag for tag, _ in plan.parents]
    changed: list[Path] = []
    for directory in [*(d for _, d in plan.parents), plan.dev_container]:
        dockerfile = Path(os.path.realpath(directory)) / DOCKERFILE
        if not dockerfile.is_relative_to(staged) or not dockerfile.is_file():
            continue
        text = dockerfile.read_text(encoding="utf-8")
        new = rewrite_images(text, images, local)
        if new != text:
            dockerfile.write_text(new, encoding="utf-8")
            changed.append(dockerfile)
    return changed


def rewrite_images(text: str, images: Mapping[str, str], local_tags: Iterable[str]) -> str:
    """`text`, a Dockerfile, with each `FROM` image in `images` replaced by its mapping.

    The image is matched as the review listed it: not a local parent's tag
    or an earlier stage.  A `FROM` that continues over lines isn't rewritten:
    it's refused, rather than guessed at.
    """
    changes = {old: new for old, new in images.items() if old != new}
    if not changes:
        return text
    skip = set(local_tags) | {"scratch"}
    stages: set[str] = set()
    out: list[str] = []
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not _FROM.match(stripped):
            out.append(line)
            i += 1
            continue
        if stripped.endswith("\\"):
            end = i
            while end < len(lines) - 1 and lines[end].rstrip().endswith("\\"):
                end += 1
            joined = " ".join(part.strip().rstrip("\\") for part in lines[i:end + 1])
            if any(word in changes for word in joined.split()):
                raise StagingErr(
                    f"line {i + 1}: a `FROM` continued over lines can't have its image replaced"
                )
            out.extend(lines[i:end + 1])
            i = end + 1
            continue
        out.append(_rewrite_from(line, changes, skip, stages))
        stage = _STAGE.search(stripped)
        if stage:
            stages.add(stage.group(1).lower())
        i += 1
    return "".join(out)


def _rewrite_from(line: str, changes: Mapping[str, str], skip: set[str], stages: set[str]) -> str:
    words = line.split()
    image = next((w for w in words[1:] if not w.startswith("--")), None)
    if image is None or image not in changes or image in skip or image.lower() in stages:
        return line
    # The image is the first word after the flags; replace that word only.
    pattern = re.compile(r"(?<=\s)" + re.escape(image) + r"(?=\s|$)")
    return pattern.sub(lambda _: changes[image], line, count=1)


_CHANGED = (
    "The project's files changed while `scoter` was starting: the accepted copy's {what} "
    "isn't the one this run read.  Run again."
)


def _files_dir(config: UserConfig) -> str:
    files_dir = config.optional_value("scoter", "project-files-dir")
    if files_dir is None:
        raise StagingErr("the setup names no `scoter: values: project-files-dir`")
    return files_dir


def prepare(
    config: UserConfig,
    dest: Path,
    generate: Callable[[UserConfig, list[str], Path], tuple[jdata.DictJsonData | None, list[str]]],
    now: datetime.datetime,
) -> BuildPlan | None:
    """Stage the accepted copy in `dest` (empty, private), check it, and plan the build.

    None when there's no acceptance.  `generate` runs the generator
    (`generator.run`).
    """
    accepted = find_accepted(config, now)
    if accepted is None:
        return None
    # Under an exemption, the review's checks on the container files haven't
    # seen them: they run here, on the live files (for what a copy can't
    # show, such as links) and on the copy (for what's built from).
    live_findings = checks.check_project_files(config) if accepted.exemption is not None else []
    staged_project = stage(config, accepted, dest)
    staged = _generate(config, staged_project, dest, generate)
    if accepted.exemption is not None:
        refused = list({
            (f.key, f.message): f for f in [*live_findings, *checks.check_project_files(staged)]
            if f.severity == checks.Severity.REFUSE
        }.values())
        if refused:
            raise StagingErr(
                "Under your exemption, the container files are used as they are, but the setup's "
                "checks refuse them:\n" + "\n".join(f"  {f.key}: {f.message}" for f in refused),
                reverify=False,
            )
    plan = stage_containers(config, accepted, build_plan(config, staged, staged_project), dest)
    apply_images(plan, dest, config.accepted_images())
    return plan
