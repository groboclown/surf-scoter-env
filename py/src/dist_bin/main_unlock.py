#SPDX:MIT
"""Copy your current project configuration into a space for active development.

A Surf Scoter sub-command.

Copy the configuration in use, or a distribution template (`--from-template
NAME`), into `${pull:paths:unlocked-dir}/ENV-ID` for you to edit.  `scoter run`
then runs it, with a warning; `scoter lock` reviews and stores it, and
`scoter lock --revert` throws the edits away.
"""
import sys
from collections.abc import Sequence

from surf_scoter_lib import BAD_EXECUTION_ARGS_CODE, gen_parser, review
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.pull import lifecycle


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "Copy your configuration in use, or a template, into the unlocked directory for you to edit."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE
    parser.add_argument(
        "--from-template", metavar="NAME", default=None,
        help="Start from the distribution's template NAME instead of your configuration in use.",
    )
    parser.add_argument(
        "--replace", action="store_true",
        help="When the unlocked directory already holds files, keep them as a backup beside it, and go on.",
    )
    args = parser.parse_args(argv)
    try:
        root = lifecycle.unlock(config, template=args.from_template, replace=args.replace)
    except (OSError, SurfScoterErr) as err:
        print(review.clean(str(err)), file=sys.stderr)
        return 1
    print(f"Unlocked: edit {root / 'files'}.  `scoter lock` reviews and stores it; `scoter lock --revert` drops it.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
