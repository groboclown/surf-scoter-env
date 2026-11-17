#SPDX:MIT
"""Find the root of a repository from its files alone.

The VCS itself is never run (`git rev-parse --show-toplevel`, say, reads the
repository's configuration, which the project controls), and no environment
variable (such as GIT_DIR) is used.  A VCS's setup section calls one of the
`vcs_root_*.py` scripts, which use this, with the name of the file or
directory that marks its checkouts.

The search goes up from the start a directory at a time.  It stops below the
home directory, so a home directory kept in a repository isn't every
project's repository, and at a file system boundary.
"""
import os
from pathlib import Path


def check_marker(marker: str) -> str | None:
    """Why `marker` isn't a usable marker name, if it isn't."""
    if not marker or marker in (".", "..") or "/" in marker or "\\" in marker:
        return "the marker must be a single file or directory name"
    return None


def search_dirs(start: Path, home: Path | None = None) -> list[Path]:
    """`start` (resolved) and the directories above it, as far as the search goes."""
    start = Path(os.path.realpath(start))
    real_home = Path(os.path.realpath(home if home is not None else Path.home()))
    try:
        device = start.stat().st_dev
    except OSError:
        return []
    ret: list[Path] = []
    for directory in [start, *start.parents]:
        if real_home.is_relative_to(directory):
            break
        try:
            if directory.stat().st_dev != device:
                break
        except OSError:
            break
        ret.append(directory)
    return ret


def nearest(marker: str, start: Path, home: Path | None = None) -> Path | None:
    """The nearest directory, at or above `start`, holding `marker`.

    For Git (`.git`, a directory, or a file in a worktree or submodule, so a
    submodule is its own root), Mercurial (`.hg`) and Subversion (`.svn`).
    """
    for directory in search_dirs(start, home):
        if os.path.lexists(directory / marker):
            return directory
    return None


def outermost(marker: str, start: Path, home: Path | None = None) -> Path | None:
    """The highest directory of the unbroken run, from the nearest one up, holding `marker`.

    For a VCS that keeps a directory in every directory of a checkout, such as
    CVS with its `CVS` directories.
    """
    root: Path | None = None
    for directory in search_dirs(start, home):
        if os.path.lexists(directory / marker):
            root = directory
        elif root is not None:
            break
    return root
