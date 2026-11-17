#SPDX:MIT
"""Offer your personal configuration for this project into the project directory.

A Surf Scoter sub-command.

Offer your configuration in use back to the project: show what it changes in
the project's files, as a patch, and write them once you approve.  It never
deletes a file, never writes the host file, and leaves committing to your
VCS.  For project owners.
"""
import sys
from collections.abc import Sequence

from surf_scoter_lib import BAD_EXECUTION_ARGS_CODE, gen_parser, review
from surf_scoter_lib.argutil import can_ask
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.pull import push


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "Write your configuration in use into the project's files, after showing the patch."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE
    parser.add_argument(
        "--yes", action="store_true",
        help="Write without showing the patch and asking.  The warnings are still printed.",
    )
    parser.add_argument("--name", default=None, help="Push this configuration instead of the one in use.")
    args = parser.parse_args(argv)
    if not args.yes and not can_ask():
        print("Push shows a patch and asks first, which needs a terminal; give --yes to write without asking.",
              file=sys.stderr)
        return 1
    try:
        return push.push(config, name=args.name, yes=args.yes)
    except (OSError, SurfScoterErr) as err:
        print(review.clean(str(err)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
