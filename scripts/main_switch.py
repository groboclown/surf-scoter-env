#SPDX:MIT
"""Switch this project directory to a different configuration.

A Surf Scoter sub-command.

Switch the project to another of its locked configurations, by name: `scoter
switch default.previous` goes back to the one a pull replaced.  The checks a run
makes run first.
"""
import sys
from collections.abc import Sequence

from surf_scoter_lib import (
    BAD_EXECUTION_ARGS_CODE,
    gen_parser,
    generator,
    review,
    run_config,
)
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.pull import lifecycle


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, "Switch the project to another of its locked configurations.")
    if not config:
        return BAD_EXECUTION_ARGS_CODE
    parser.add_argument("name", metavar="NAME", help="The configuration's name.")
    args = parser.parse_args(argv)
    try:
        chosen = lifecycle.switch(config, args.name, generate=generator.run)
    except (OSError, SurfScoterErr) as err:
        print(str(err) if isinstance(err, run_config.RunRefused) else review.clean(str(err)), file=sys.stderr)
        return 1
    print(f"Now using `{review.clean(chosen.name)}` ({chosen.id}).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
