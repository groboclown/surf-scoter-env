#SPDX:MIT
"""Print the root of the repository holding a directory: the nearest directory with a marker.

Usage: vcs_root_nearest.py MARKER START

A VCS's setup section runs this as its `runs: find-root`.  It prints the
root, or nothing when START isn't in a repository; see
`surf_scoter_lib.vcs_root`.
"""
import sys
from collections.abc import Sequence
from pathlib import Path

from surf_scoter_lib import vcs_root


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    if len(argv) != 3:
        print(f"Usage: {argv[0] if argv else 'vcs_root_nearest.py'} MARKER START", file=sys.stderr)
        return 2
    marker, start = argv[1], argv[2]
    problem = vcs_root.check_marker(marker)
    if problem:
        print(f"{argv[0]}: {problem}: {marker!r}", file=sys.stderr)
        return 2
    root = vcs_root.nearest(marker, Path(start))
    if root is not None:
        print(root)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
