#SPDX:MIT
"""Review your current project configuration for potential security issues.

A Surf Scoter sub-command.

Review the configuration in use again: what's new around it leads (findings
new since the last review, programs resolved again), the rest folded.
Approving stores it again and resets the review timers; denying changes
nothing.
"""
import argparse
import sys
from collections.abc import Sequence

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
    config, argv, parser = gen_parser(argv, "Review the configuration in use again.")
    if not config:
        return BAD_EXECUTION_ARGS_CODE
    _review_options(parser)
    args = parser.parse_args(argv)
    try:
        return lifecycle.review_in_use(config, porcelain.Options(
            allow_opaque=args.allow_opaque, keep_working_dir=args.keep_working_dir,
        ))
    except (OSError, SurfScoterErr) as err:
        print(review.clean(str(err)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
