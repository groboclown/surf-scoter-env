#SPDX:MIT
"""Surf Scoter Sub-command: fetch"""
import sys
from collections.abc import Sequence

from surf_scoter_lib import BAD_EXECUTION_ARGS_CODE, UserConfig, gen_parser, log


def run_fetch(config: UserConfig, *, branch: bool | None) -> int:
    """Run the fetch operation"""
    log.error("Not implemented")
    return 1


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "Fetch changes committed by the dev container to its local workspace."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE

    parser.add_argument(
        "-b",
        "--branch",
        action="store",
        default="workspace-fetch",
        dest="branch",
        help=(
            "The branch name to fetch the workspace changes into."
        ),
    )

    args = parser.parse_args(argv)
    return run_fetch(config, branch=args.branch)

if __name__ == "__main__":
    sys.exit(main(sys.argv))
