#SPDX:MIT
"""Print the branch checked out in a Git repository, from its files.

Usage: vcs_git_branch.py ROOT

The `git-vcs` setup section runs this as its `runs: current-branch`, for the
review.  It prints the branch, `detached at COMMIT`, or nothing when it
can't tell; see `surf_scoter_lib.vcs_branch`.  Git isn't run.
"""
import sys
from collections.abc import Sequence
from pathlib import Path

from surf_scoter_lib import vcs_branch


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    if len(argv) != 2:
        print(f"Usage: {argv[0] if argv else 'vcs_git_branch.py'} ROOT", file=sys.stderr)
        return 2
    branch = vcs_branch.current_branch(Path(argv[1]))
    if branch is not None:
        print(branch)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
