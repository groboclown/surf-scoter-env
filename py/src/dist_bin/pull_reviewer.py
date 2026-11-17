#SPDX:MIT
"""The default reviewer for `scoter pull`: explore the working directory in the terminal.

Usage: pull_reviewer.py UNIVERSAL WORKDIR-ID SESSION

The setup's `pull: runs: reviewer` runs this, given the porcelain's SUC, the
working directory's id and the session file, which it replaces with the
decision.  Both the input and the output must be terminals: it reviews for
a person, and has no `--yes` (see `surf_scoter_lib.pull.reviewer`).
"""
import sys
from collections.abc import Sequence
from pathlib import Path

from surf_scoter_lib import UserConfig, review
from surf_scoter_lib.argutil import can_ask
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.pull import reviewer


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    if len(argv) != 4:
        print("Usage: pull_reviewer.py UNIVERSAL WORKDIR-ID SESSION", file=sys.stderr)
        return 2
    if not can_ask():
        print(
            "The review needs a terminal for its input and output.  Automation provides its own "
            "files; a person vets them with `scoter pull`.",
            file=sys.stderr,
        )
        return 2
    try:
        decided = reviewer.review_working_dir(
            UserConfig.read(Path(argv[1])), argv[2], Path(argv[3]), input, sys.stdout,
        )
    except (OSError, SurfScoterErr) as err:
        print(review.clean(str(err)), file=sys.stderr)
        return 1
    return 0 if decided.result == reviewer.APPROVE else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
