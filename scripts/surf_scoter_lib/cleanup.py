#SPDX:MIT
"""Find, and remove, what Surf Scoter leaves behind (`scoter cleanup`).

`scoter kill` and `scoter revoke` work on what the user names.  This finds
what nothing else removes:

* stale run records: environments whose `scoter run` process is gone (the
  script stops them as `scoter kill` does);
* orphaned state: a run's `environments/HOST` directory with no record, and
  the `exempt-*` directories a run under an exemption makes for a moment
  that one stopped part way left;
* gone projects: the verified store's projects whose directory is gone;
* the store's leftovers: directories no record names, blobs nothing uses,
  temporary files;
* with `project_state`, the project state directories (each project's
  clone, settings and resources) no running environment uses;
* with `persistent_stores`, the persistent store volumes
  (`project-state: values: persistent-store-prefix`) no running environment
  uses;
* with `images`, the images (`dev-env: values: dev-env-image-prefix`) no
  running environment uses.

Each `find_*` only reads; each `remove_*` looks again under the lock that
guards what it removes, and removes only what's still as it was found.
"""
import datetime
import os
import stat
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import fileutil, persistent_store
from .active_env import ActiveEnvironmentSet, EnvironmentDesc
from .user_config import Echo, OnError, UserConfig
from .verified import records, revoke, store

# Where the run keeps each environment's files (`main_run.SUC_COPY_DIR`),
# and, by default, each project's state (its clone, settings and resources).
ENVIRONMENTS_DIR = "environments"
PROJECTS_DIR = "projects"
EXEMPT_PREFIX = "exempt-"
# An `exempt-*` directory lasts as long as a run takes to read its project's
# files; one older than this was left by a run that stopped.
EXEMPT_AGE = datetime.timedelta(hours=1)


def process_alive(pid: int) -> bool | None:
    """Whether the process `pid` is running on this computer; None when that can't be told.

    On Windows, and for a record with no process (`pid` 0), it can't.
    """
    if pid <= 0 or fileutil.is_windows():
        return None
    try:
        # Signal 0 only checks; on Windows `os.kill` would end the process.
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process has the number now: not the run.
        return False
    except OSError:
        return None
    return True


def dir_size(path: Path) -> int:
    """The bytes the files under `path` take, not following links."""
    total = 0
    for directory, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(Path(directory) / name).st_size
            except OSError:
                pass
    return total


def _real(path: str | Path) -> Path:
    return Path(os.path.realpath(path))


# ---------------------------------------------------------------------
# Run records and the run's state


@dataclass
class Runs:
    """The run records: those still running, and the stale ones."""

    running: list[EnvironmentDesc] = field(default_factory=list)
    stale: list[EnvironmentDesc] = field(default_factory=list)


def _envs_file(config: UserConfig) -> tuple[Path, Path]:
    return config.require_path("scoter", "running-envs"), config.require_path("scoter", "running-envs-lock")


def find_runs(config: UserConfig) -> Runs:
    """The run records, split by whether their `scoter run` is still running.

    A record whose process can't be checked (see `process_alive`) counts as
    running: only `scoter kill` stops it.
    """
    file, _ = _envs_file(config)
    runs = Runs()
    for env in ActiveEnvironmentSet.read(file).environs.values():
        (runs.stale if process_alive(env.pid) is False else runs.running).append(env)
    return runs


def is_stale(config: UserConfig, host: str) -> bool:
    """Whether `host`'s record is still there, and still stale."""
    file, _ = _envs_file(config)
    env = ActiveEnvironmentSet.read(file).get_environ_by_host(host)
    return env is not None and process_alive(env.pid) is False


def find_orphaned_state(config: UserConfig, now: datetime.datetime) -> list[Path]:
    """The run's state no record uses: `environments/HOST` directories, and old `exempt-*` ones."""
    state_dir = config.require_path("scoter", "state-dir")
    file, _ = _envs_file(config)
    return _orphaned_state(state_dir, ActiveEnvironmentSet.read(file), now)


def _orphaned_state(state_dir: Path, envs: ActiveEnvironmentSet, now: datetime.datetime) -> list[Path]:
    hosts = {env.host for env in envs.environs.values()}
    found: list[Path] = []
    environments = state_dir / ENVIRONMENTS_DIR
    if environments.is_dir() and not environments.is_symlink():
        found.extend(
            environments / name for name in sorted(os.listdir(environments)) if name not in hosts
        )
    if state_dir.is_dir():
        for name in sorted(os.listdir(state_dir)):
            path = state_dir / name
            if not name.startswith(EXEMPT_PREFIX):
                continue
            st = os.lstat(path)
            changed = datetime.datetime.fromtimestamp(st.st_mtime, datetime.UTC)
            if stat.S_ISDIR(st.st_mode) and now - changed > EXEMPT_AGE:
                found.append(path)
    return found


def remove_orphaned_state(config: UserConfig, paths: Iterable[Path], now: datetime.datetime) -> list[Path]:
    """Remove the paths `find_orphaned_state` found, that still are; those removed.

    Under the running environments' lock, so no run reserves a host meanwhile.
    """
    state_dir = config.require_path("scoter", "state-dir")
    file, lock_file = _envs_file(config)
    removed: list[Path] = []
    with ActiveEnvironmentSet.locked(file, lock_file) as envs:
        still = set(_orphaned_state(state_dir, envs, now))
        for path in paths:
            if path in still and fileutil.rm(path):
                removed.append(path)
    return removed


# ---------------------------------------------------------------------
# The verified store


@dataclass
class StoreLeftovers:
    """What the verified store holds that no record names."""

    # Environment directories the index doesn't list, or whose record is gone.
    environments: list[str] = field(default_factory=list)
    # Acceptance directories their environment's record doesn't list: (env id, acceptance id).
    acceptances: list[tuple[str, str]] = field(default_factory=list)
    unused_blobs: int = 0
    temp_files: int = 0
    size: int = 0

    def __bool__(self) -> bool:
        return bool(self.environments or self.acceptances or self.unused_blobs or self.temp_files)

    def describe(self) -> list[str]:
        """A line for each kind of leftover."""
        lines = [f"environment directory {e} (no project names it)" for e in self.environments]
        lines += [f"acceptance directory {e}/{a} (its record doesn't name it)" for e, a in self.acceptances]
        if self.unused_blobs:
            lines.append(f"{self.unused_blobs} copied file{'s' if self.unused_blobs != 1 else ''} no acceptance uses")
        if self.temp_files:
            lines.append(f"{self.temp_files} temporary file{'s' if self.temp_files != 1 else ''} from a write that stopped")
        return lines


_STORE_FILES = {store.INDEX_FILE, store.LOCK_FILE, store.BLOBS_DIR}


def find_gone_projects(verified_store: store.VerifiedStore) -> list[revoke.Environment]:
    """The store's projects whose directory is gone."""
    return [env for env in revoke.environments(verified_store) if not os.path.isdir(env.project_dir)]


def remove_gone_project(env: revoke.Environment) -> list[records.Acceptance]:
    """Revoke every acceptance of a project whose directory is still gone; those revoked."""
    if os.path.isdir(env.project_dir):
        return []
    record = env.record()
    if record is None:
        with env.store.locked():
            env.store.unregister(env.project_dir)
            env.store.remove(env.env_id)
        return []
    return revoke.revoke(env, [a.id for a in record.acceptances])


def find_store_leftovers(verified_store: store.VerifiedStore) -> StoreLeftovers:
    """What the store holds that no record names (it's read, not changed)."""
    found = StoreLeftovers()
    index = verified_store.index()
    known = set(index.values())
    used: set[str] = set()
    for name in verified_store.list_dir():
        if name in _STORE_FILES:
            continue
        if name not in known:
            found.environments.append(name)
            found.size += dir_size(verified_store.root / name)
    for project_dir, env_id in sorted(index.items()):
        record = records.load_environment(verified_store, project_dir, env_id)
        if record is None:
            if verified_store.list_dir(env_id):
                found.environments.append(env_id)
                found.size += dir_size(verified_store.root / env_id)
            continue
        used |= record.blobs()
        ids = {a.id for a in record.acceptances}
        for acceptance in verified_store.list_dir(env_id, records.ACCEPTANCES_DIR):
            if acceptance not in ids:
                found.acceptances.append((env_id, acceptance))
                found.size += dir_size(verified_store.root / env_id / records.ACCEPTANCES_DIR / acceptance)
    for prefix in verified_store.list_dir(store.BLOBS_DIR):
        for digest in verified_store.list_dir(store.BLOBS_DIR, prefix):
            if digest not in used:
                found.unused_blobs += 1
                found.size += os.lstat(verified_store.root / store.BLOBS_DIR / prefix / digest).st_size
    temps = verified_store.temp_files()
    found.temp_files = len(temps)
    found.size += sum(os.lstat(p).st_size for p in temps)
    return found


def remove_store_leftovers(verified_store: store.VerifiedStore) -> StoreLeftovers:
    """Remove what the store holds that no record names, as it is now; what went."""
    with verified_store.locked():
        found = find_store_leftovers(verified_store)
        for env_id in found.environments:
            verified_store.remove(env_id)
        for env_id, acceptance in found.acceptances:
            verified_store.remove(env_id, records.ACCEPTANCES_DIR, acceptance)
        # The records now name only what's there; then what none of them use.
        found.unused_blobs = records.remove_unused_blobs(verified_store)
        found.temp_files = verified_store.remove_temp_files()
    return found


# ---------------------------------------------------------------------
# Project state


def project_state_root(config: UserConfig) -> Path:
    """Where the projects' state directories are, by default: `${scoter:state-dir}/projects`."""
    return config.require_path("scoter", "state-dir") / PROJECTS_DIR


def _in_use(envs: Iterable[EnvironmentDesc]) -> tuple[list[Path], set[str]]:
    """The directories the runs use, and the project ids of the runs still starting."""
    used: list[Path] = []
    starting: set[str] = set()
    for env in envs:
        used.extend(_real(d) for d in (env.project_clone_dir, env.setup_dir, env.resources_dir) if d)
        if env.project_dir:
            # A run that's starting hasn't recorded its clone yet.
            starting.add(store.environment_id(_real(env.project_dir)))
    return used, starting


def _unused_project_state(root: Path, envs: Iterable[EnvironmentDesc]) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    used, starting = _in_use(envs)
    found: list[Path] = []
    for name in sorted(os.listdir(root)):
        path = root / name
        if path.is_symlink() or not path.is_dir() or name in starting:
            continue
        real = _real(path)
        if any(u == real or u.is_relative_to(real) for u in used):
            continue
        found.append(path)
    return found


def find_project_state(config: UserConfig, keep: Iterable[EnvironmentDesc]) -> list[Path]:
    """The project state directories (under `project_state_root`) the runs `keep` don't use."""
    return _unused_project_state(project_state_root(config), keep)


def remove_project_state(config: UserConfig, paths: Iterable[Path]) -> list[Path]:
    """Remove the directories `find_project_state` found, that still aren't used; those removed.

    Under the running environments' lock, so no run starts meanwhile.
    """
    file, lock_file = _envs_file(config)
    removed: list[Path] = []
    with ActiveEnvironmentSet.locked(file, lock_file) as envs:
        still = set(_unused_project_state(project_state_root(config), envs.environs.values()))
        for path in paths:
            if path in still and fileutil.rm(path):
                removed.append(path)
    return removed


# ---------------------------------------------------------------------
# Persistent stores


def persistent_store_prefix(config: UserConfig) -> str:
    """The prefix every persistent store volume's name starts with."""
    prefix = config.require_value("project-state", "persistent-store-prefix")
    if not prefix or not persistent_store.VOLUME_NAME.fullmatch(prefix):
        raise OSError(f"`project-state: values: persistent-store-prefix` isn't a volume name prefix: {prefix!r}")
    return prefix


def stores_in_use(envs: Iterable[EnvironmentDesc]) -> set[str]:
    """The persistent store volumes the runs `envs` use, from the configuration each ran with.

    A run that's only reserved (or starting) has none recorded: its project's
    volume is kept by its project id instead (`_starting_ids`).
    """
    used: set[str] = set()
    for env in envs:
        if env.suc:
            volume = UserConfig(env.suc).optional_value("project-state", "persistent-store-volume")
            if volume:
                used.add(volume)
    return used


def _starting_ids(envs: Iterable[EnvironmentDesc]) -> set[str]:
    return {store.environment_id(_real(env.project_dir)) for env in envs if env.project_dir}


def find_persistent_stores(config: UserConfig, keep: Iterable[EnvironmentDesc]) -> list[str]:
    """The persistent store volumes the runs `keep` don't use, from the container tool."""
    prefix = persistent_store_prefix(config)
    res = config.run_command(
        "docker", "list-volumes", on_error=OnError.RETURN, echo=Echo.QUIET,
        template={"prefix": prefix},
    )
    if res.returncode != 0:
        raise OSError("the container tool couldn't list its volumes")
    listed = list(keep)
    used = stores_in_use(listed)
    starting = _starting_ids(listed)
    names = [line.strip() for line in res.stdout.decode("utf-8", errors="replace").splitlines()]
    return sorted({
        name for name in names
        if name.startswith(prefix) and name not in used
        and not any(project_id in name for project_id in starting)
    })


def remove_persistent_stores(config: UserConfig, names: Iterable[str]) -> list[str]:
    """Remove the volumes `find_persistent_stores` found, that still aren't used; those removed."""
    file, lock_file = _envs_file(config)
    removed: list[str] = []
    with ActiveEnvironmentSet.locked(file, lock_file) as envs:
        listed = list(envs.environs.values())
        used = stores_in_use(listed)
        starting = _starting_ids(listed)
        for name in names:
            if name in used or any(project_id in name for project_id in starting):
                continue
            if persistent_store.remove(config, name):
                removed.append(name)
            else:
                print(f"Couldn't remove the volume {name}.", file=sys.stderr)
    return removed


# ---------------------------------------------------------------------
# Images


def image_prefix(config: UserConfig) -> str:
    """The prefix of the images the run builds (`dev-env: values: dev-env-image-prefix`)."""
    return config.require_value("dev-env", "dev-env-image-prefix")


def images_in_use(config: UserConfig, envs: Iterable[EnvironmentDesc]) -> set[str]:
    """The images the runs `envs` use: each one's dev container, and the network's when any runs."""
    listed = list(envs)
    prefix = image_prefix(config)
    used = {prefix + env.host for env in listed}
    if listed:
        used |= {
            config.require_value("dev-env", "firewall-router-image"),
            config.require_value("dev-env", "network-gateway-image"),
        }
    # An image with no tag given is `:latest`.
    return used | {f"{name}:latest" for name in used if ":" not in name.rsplit("/", 1)[-1]}


def find_images(config: UserConfig, keep: Iterable[EnvironmentDesc]) -> list[str]:
    """The run's images (`REPOSITORY:TAG`) the runs `keep` don't use, from the container tool."""
    res = config.run_command(
        "docker", "list-images", on_error=OnError.RETURN, echo=Echo.QUIET,
        template={"pattern": image_prefix(config) + "*"},
    )
    if res.returncode != 0:
        raise OSError("the container tool couldn't list its images")
    used = images_in_use(config, keep)
    names = [line.strip() for line in res.stdout.decode("utf-8", errors="replace").splitlines()]
    return sorted({
        name for name in names
        if name and "<none>" not in name and name.startswith(image_prefix(config)) and name not in used
    })


def remove_images(config: UserConfig, names: Iterable[str]) -> list[str]:
    """Remove the images, then the layers no image uses; those removed."""
    file, _ = _envs_file(config)
    used = images_in_use(config, ActiveEnvironmentSet.read(file).environs.values())
    removed: list[str] = []
    for name in names:
        if name in used:
            continue
        res = config.run_command(
            "docker", "remove-image", on_error=OnError.RETURN, echo=Echo.QUIET,
            template={"name": name},
        )
        if res.returncode == 0:
            removed.append(name)
        else:
            print(f"Couldn't remove the image {name}.", file=sys.stderr)
    config.run_command("docker", "prune-images", on_error=OnError.RETURN, echo=Echo.QUIET)
    return removed
