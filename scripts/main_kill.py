#SPDX:MIT
"""Surf Scoter Sub-command: kill"""
from collections.abc import Sequence
import sys

from surf_scoter_lib import log, gen_parser, BAD_EXECUTION_ARGS_CODE, UserConfig


def run_kill(
    config: UserConfig,
    *,
    host: str | None,
    fetch_branch: str | None,
    force_clean: bool,
) -> int:
    """Run the kill operation"""
    log.error("Not implemented")
    return 1


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "Stop the project dev container.  The 'run' should stop the configurations"
        "when it terminates, but unexpected issues can leave dangling resources.  "
        "Use this to stop run-away configurations."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE

    # Add arguments to the parser
    parser.add_argument(
        "--host",
        action="store",
        dest="host",
        help="Force a specific host to stop.  Doesn't need the project.",
    )
    parser.add_argument(
        "-f",
        "--fetch",
        action="store",
        dest="fetch_branch",
        help=(
            "Fetch the files from the workspace into your project directory into this branch."
        ),
    )
    parser.add_argument(
        "--force-clean",
        action="store_true",
        default=False,
        dest="force_clean",
        help=(
            "Force delete of the workspace files.  Setting this can potentially"
            "delete changes, so you should use this with the '--fetch' argument."
        ),
    )

    args = parser.parse_args(argv)
    return run_kill(
        config,
        host=args.host,
        fetch_branch=args.fetch_branch,
        force_clean=args.force_clean,
    )

if __name__ == "__main__":
    sys.exit(main(sys.argv))
