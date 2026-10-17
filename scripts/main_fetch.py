#SPDX:MIT
"""Surf Scoter Sub-command: fetch"""


# ---------------------------------------------------------------------
# Fetch changes from a dev environment.

def fetch_changes_parser(parser: argparse.ArgumentParser) -> None:
    """Configure the CLI arguments."""
    parser.description = "Fetch changes committed by the dev container to its local workspace."
    parser.set_defaults(func=fetch_changes_runner)
    _add_project_args(parser)
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

# TODO additional commands to use with this:
#      review, merge.
SUB_COMMAND_ARGUMENTS["fetch"] = fetch_changes_parser


def fetch_changes_runner(args: argparse.Namespace) -> int:
    """Fetch in changes from the dev environment."""
    config = UserConfig.load_project_from_args(args)

    # FIXME
    assert config is not None

    return 0
