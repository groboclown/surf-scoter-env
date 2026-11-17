#SPDX:MIT
"""The unlocked copy: where it is, whether it can be used, and reading it.

`${pull:paths:unlocked-dir}/ENV-ID` holds a configuration for the developer
to edit: `files/`, and `host.json` beside it (VER-42).  It's never built
from where it is: a run, and `scoter lock`, read it as seeding reads a
source (links, special files and the limits refused), then stage that.

The setting is refused inside the project, its repository, the project
clone, the state directory and the store (VER-37): the container could
write its own settings for the next run, or the project's files would run
unreviewed.  The copy must be the developer's alone, with nothing others can
write, checked on every use.
"""
import os
import stat
from pathlib import Path

from .. import fileutil, host_file, jdata
from ..err import SurfScoterErr
from ..user_config import FoundProject, UserConfig
from ..verified import snapshot

FILES_DIR = "files"


class UnlockedErr(SurfScoterErr):
    """The unlocked copy, or its setting, can't be used."""


def root(config: UserConfig, env_id: str) -> Path:
    """`${pull:paths:unlocked-dir}/ENV-ID`, after checking the setting (VER-37)."""
    base = config.optional_path("pull", "unlocked-dir")
    if base is None:
        raise UnlockedErr("The setup names no `pull: paths: unlocked-dir`.")
    check_location(config, base, "`pull: paths: unlocked-dir`")
    return base / env_id


def forbidden_locations(config: UserConfig, found: FoundProject | None) -> dict[str, Path]:
    """The places the tools' own copies can't be: the project and its repository and clone,
    the state directory and the store, by name, with symlinks resolved."""
    places: dict[str, Path | None] = {
        "scoter:state-dir": config.optional_path("scoter", "state-dir"),
        "verification:store": config.optional_path("verification", "store"),
    }
    if found is not None:
        places["the project directory"] = found.project_dir
        places["its repository"] = found.vcs_root
    for name in ("project-dir", "vcs-root", "project-clone-dir"):
        places[f"scoter:{name}"] = config.active_location(name)
    return {name: Path(os.path.realpath(path)) for name, path in places.items() if path is not None}


def check_location(
    config: UserConfig, path: Path, what: str, *, found: FoundProject | None = None,
    also: dict[str, Path] | None = None,
) -> None:
    """Refuse `path` inside (or holding) one of the forbidden places, or `also`'s."""
    found = found if found is not None else config.found_project()
    real = Path(os.path.realpath(path))
    places = {**forbidden_locations(config, found), **{k: Path(os.path.realpath(v)) for k, v in (also or {}).items()}}
    for name, place in places.items():
        if real == place or real.is_relative_to(place):
            raise UnlockedErr(
                f"{what} ({path}) is inside {name} ({place}): files there could be taken for "
                "configuration, or reach the container.  Choose a directory outside it."
            )


def check_owned(directory: Path) -> None:
    """The copy must be yours, with nothing in it others can write."""
    if fileutil.is_windows():
        return
    for current, dirs, names in os.walk(directory):
        for path in [Path(current), *(Path(current) / n for n in [*dirs, *names])]:
            st = os.lstat(path)
            if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
                raise UnlockedErr(
                    f"{path}: in your unlocked configuration, but not yours alone (another user owns "
                    "it, or others can write it); fix its owner and mode, then try again"
                )


def read(directory: Path, file_name: str, limits: snapshot.SnapshotLimits) -> tuple[snapshot.Snapshot, jdata.DictJsonData | None]:
    """The unlocked copy at `directory`: its files, as a source snapshot, and its host file, if it has one."""
    if not (directory / FILES_DIR).is_dir():
        raise UnlockedErr(f"{directory}: your unlocked configuration isn't there; `scoter lock --revert` goes back")
    check_owned(directory)
    source = snapshot.snapshot_source(directory / FILES_DIR, file_name, limits)
    host = directory / host_file.FILE_NAME
    return source, (host_file.read(host) if os.path.lexists(host) else None)


def holds_files(directory: Path) -> bool:
    """Whether the directory is there, and isn't empty."""
    return directory.is_dir() and bool(os.listdir(directory))
