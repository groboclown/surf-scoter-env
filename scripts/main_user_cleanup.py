#SPDX:MIT
"""Surf Scoter Sub-command: user-cleanup"""
from collections.abc import Sequence
import sys

from surf_scoter_lib import log, gen_parser, BAD_EXECUTION_ARGS_CODE, UserConfig


def run_cleanup_user(config: UserConfig) -> int:
    """Clean up the surf scoter install in the user's environment."""
    log.error("Not implemented")
    return 1


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "Cleanup the files that this added or modified for the dev "
        "environments from your user directories."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE

    args = parser.parse_args(argv)
    return run_cleanup_user(config)

if __name__ == "__main__":
    sys.exit(main(sys.argv))
