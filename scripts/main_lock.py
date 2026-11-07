#SPDX:MIT
"""Move the current configuration out of development mode.

A Surf Scoter sub-command.

Review your unlocked configuration, store it as a locked configuration, and
remove the unlocked copy (or rename it, with `--keep DIR`).  With
`--revert`, throw the edits away instead, and go back to the configuration
you unlocked.
"""
import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from surf_scoter_lib import BAD_EXECUTION_ARGS_CODE, gen_parser, review
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.pull import lifecycle, porcelain


def _review_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-opaque", action="append", default=[], metavar="PATH",
        help="Allow this file, which the review can't show (too large, or not text), to be approved unseen.",
    )
    parser.add_argument(
        "--keep-working-dir", action="store_true",
        help="Keep the review's working directory afterwards, for debugging; it says where.",
    )


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "Review and store your unlocked configuration, or throw your edits away with --revert."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE
    parser.add_argument(
        "--revert", action="store_true",
        help="Throw your edits away, and go back to the configuration you unlocked.  No review.",
    )
    parser.add_argument(
        "--yes", action="store_true", help="With --revert: don't ask before throwing the edits away.",
    )
    parser.add_argument(
        "--keep", metavar="DIR", default=None,
        help="Rename the unlocked copy to DIR, a new directory outside the project and the tools' own, instead of removing it.",
    )
    parser.add_argument("--name", default=None, help="The configuration's name (default: the one you unlocked's).")
    _review_options(parser)
    args = parser.parse_args(argv)
    keep = None if args.keep is None else Path(args.keep).absolute()
    try:
        if args.revert:
            return lifecycle.revert(config, keep=keep, yes=args.yes)
        return lifecycle.lock(config, porcelain.Options(
            name=args.name, allow_opaque=args.allow_opaque, keep_working_dir=args.keep_working_dir,
        ), keep=keep)
    except (OSError, SurfScoterErr) as err:
        print(review.clean(str(err)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
