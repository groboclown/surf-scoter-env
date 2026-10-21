#SPDX:MIT
"""The current branch of a Git checkout, read from its files.

Git itself isn't run: it would read the repository's configuration, which
the project controls.  `.git` is a directory, or (in a worktree or a
submodule) a file naming one with `gitdir: PATH`.  Its `HEAD` is
`ref: refs/heads/NAME` on a branch, or a commit id when detached.
"""
import os
import stat
from pathlib import Path

MAX_SIZE = 4096


def _read_small(path: Path) -> str | None:
    """A small regular file's text, not following a symlink; None if it isn't one."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, MAX_SIZE + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_SIZE:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def git_dir(root: Path) -> Path | None:
    """The Git directory of the checkout at `root`."""
    marker = root / ".git"
    if marker.is_dir() and not marker.is_symlink():
        return marker
    text = _read_small(marker)
    if text is None or not text.startswith("gitdir:"):
        return None
    target = Path(text[len("gitdir:"):].strip())
    return target if target.is_absolute() else root / target


def current_branch(root: Path) -> str | None:
    """The branch checked out at `root`, `detached at COMMIT`, or None if it can't be told."""
    directory = git_dir(root)
    if directory is None:
        return None
    head = _read_small(directory / "HEAD")
    if head is None:
        return None
    head = head.strip()
    if head.startswith("ref: "):
        ref = head[len("ref: "):]
        return ref.removeprefix("refs/heads/")
    if len(head) in (40, 64) and all(c in "0123456789abcdef" for c in head):
        return f"detached at {head[:12]}"
    return None
