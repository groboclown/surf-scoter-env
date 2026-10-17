#SPDX:MIT
"""Surf Scoter Sub-command: parse-config"""


# ---------------------------------------------------------------------
# Configuration debugging.

def configuration_debug_parser(parser: argparse.ArgumentParser) -> None:
    """Configure the CLI arguments."""
    parser.description = "Output the Surf Scoter universal configuration file."
    parser.set_defaults(func=configuration_debug_runner)
    _add_project_args(parser)
    parser.add_argument(
        "-n",
        "--no-project",
        action="store_false",
        default=True,
        dest="use_project",
        help="Do not try to lookup the project files.",
    )

SUB_COMMAND_ARGUMENTS["parse-config"] = configuration_debug_parser


def configuration_debug_runner(args: argparse.Namespace) -> int:
    """Output the SUC to stdout."""
    if args.use_project:
        config = UserConfig.load_project_from_args(args)
    else:
        config = UserConfig.load_settings_from_args(args)
    print(config.dumps())
    return 0
