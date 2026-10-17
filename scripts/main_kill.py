#SPDX:MIT
"""Surf Scoter Sub-command: kill"""


# ---------------------------------------------------------------------
# Stop a dev environment.

def stop_env_parser(parser: argparse.ArgumentParser) -> None:
    """Configure the CLI arguments."""
    parser.description = (
        "Stop the project dev container.  The 'run' should stop the configurations"
        "when it terminates, but unexpected issues can leave dangling resources.  "
        "Use this to stop run-away configurations."
    )
    parser.set_defaults(func=stop_env_runner)
    _add_project_args(parser)
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


SUB_COMMAND_ARGUMENTS["stop"] = stop_env_parser


def stop_env_runner(args: argparse.Namespace) -> int:
    """Stop the dev environment."""
    host = args.host
    if host:
        config = UserConfig.load_settings_from_args(args)
    else:
        config = UserConfig.load_project_from_args(args)
        # FIXME find the host by reviewing the active environment list.

    # FIXME
    assert config is not None

    return 0

