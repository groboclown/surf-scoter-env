#SPDX:MIT
"""Set up a Surf Scoter project for the current directory.

A Surf Scoter sub-command.

Start your configuration for a project with no Surf Scoter files of its own,
from a distribution template (`scoter unlock --from-template NAME`, made
friendly).  It refuses when the project already has a project files directory,
and never writes into the project: `scoter push` does that.
"""
import sys
from collections.abc import Sequence

from surf_scoter_lib import BAD_EXECUTION_ARGS_CODE, gen_parser, review
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.pull import lifecycle


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "Start your configuration from a template, for a project with no Surf Scoter files of its own."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE
    parser.add_argument(
        "name", metavar="NAME", nargs="?", default=None,
        help="The template (default: `pull: values: default-template`).",
    )
    args = parser.parse_args(argv)
    try:
        return lifecycle.init(config, args.name)
    except (OSError, SurfScoterErr) as err:
        print(review.clean(str(err)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
