#SPDX:MIT
"""Surf Scoter Sub-command: user-cleanup"""


# ---------------------------------------------------------------------
# Clean up what Surf Scoter added to the user files.

def cleanup_user_parser(parser: argparse.ArgumentParser) -> None:
    """Configure the CLI arguments."""
    parser.description = (
        "Cleanup the files that this added or modified for the dev "
        "environments from your user directories."
    )
    parser.set_defaults(func=cleanup_user_runner)


SUB_COMMAND_ARGUMENTS["user-cleanup"] = cleanup_user_parser


def cleanup_user_runner(args: argparse.Namespace) -> int:
    """Stop the dev environment."""
    config = UserConfig.load_settings_from_args(args)

    # FIXME
    assert config is not None

    return 0
