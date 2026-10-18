#SPDX:MIT
"""Surf Scoter Sub-command: parse-config"""
from collections.abc import Sequence
import sys

from surf_scoter_lib import log, gen_parser, BAD_EXECUTION_ARGS_CODE, UserConfig


def run_config_examine(config: UserConfig) -> int:
    """Handle the configuration examination."""
    log.warning(config.dumps())
    return 0


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "Fetch changes committed by the dev container to its local workspace."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE

    parser.add_argument(
        "-n",
        "--no-project",
        action="store_false",
        default=True,
        dest="use_project",
        help="Do not try to lookup the project files.",
    )

    args = parser.parse_args(argv)

    if not args.use_project:
        # This requires re-running the generator, and explicitly
        # turning off the project information.
        log.error(
            "Examining the configuration without trying to find the project "
            "isn't currently implemented.  Instead, try running this outside "
            "a project directory."
        )

    return run_config_examine(config)

if __name__ == "__main__":
    sys.exit(main(sys.argv))
