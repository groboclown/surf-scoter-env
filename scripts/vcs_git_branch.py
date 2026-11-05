#SPDX:MIT
"""Print the branch checked out in a Git repository, from its files.

Usage: vcs_git_branch.py ROOT

The `git-vcs` setup section runs this as its `runs: current-branch`, which
`scoter` runs for `${scoter:project-branch}`.  It prints the branch,
`detached at COMMIT`, or nothing when it can't tell.

Git itself isn't run: it would read the repository's configuration, which
the project controls.  `.git` is a directory, or (in a worktree or a
submodule) a file naming one with `gitdir: PATH`.  Its `HEAD` is
`ref: refs/heads/NAME` on a branch, or a commit id when detached.
"""
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

from surf_scoter_lib import fileutil

MAX_FILE_BYTES = 4096
# A commit id's length in hex digits: SHA-1, and SHA-256.
COMMIT_ID_CHARS = (40, 64)
# How much of a detached commit's id to show.
SHORT_COMMIT_CHARS = 12


def _read_small(path: Path) -> str | None:
    """A small regular file's text, not following a symlink; None if it isn't one."""
    try:
        fd = os.open(path, os.O_RDONLY | fileutil.O_NOFOLLOW | fileutil.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, MAX_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_BYTES:
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
    if len(head) in COMMIT_ID_CHARS and all(c in "0123456789abcdef" for c in head):
        return f"detached at {head[:SHORT_COMMIT_CHARS]}"
    return None


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    if len(argv) != 2:
        print(f"Usage: {argv[0] if argv else 'vcs_git_branch.py'} ROOT", file=sys.stderr)
        return 2
    branch = current_branch(Path(argv[1]))
    if branch is not None:
        print(branch)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
