#SPDX:MIT
"""Helpers for argument handling to the sub-commands."""
import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

from . import log
from .user_config import UserConfig

BAD_EXECUTION_ARGS_CODE = 124


def can_ask() -> bool:
    """Whether the user can be asked a question: both the input and the output are a terminal.

    `input` writes its prompt to the output, so with the output going to a
    file the user wouldn't see the question, and the command would wait.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def gen_parser(
    argv: Sequence[str],
    description: str,
) -> tuple[UserConfig | None, Sequence[str], argparse.ArgumentParser]:
    """Construct the user config and argument parser.
    
    General layout:

    ```
    from collections.abc import Sequence
    import sys
    from surf_scoter_lib import gen_parser, BAD_EXECUTION_ARGS_CODE

    def main(argv: Sequence[str]) -> int:
        config, argv, parser = gen_parser(argv, "Example of a command structure.")
        if not config:
            return BAD_EXECUTION_ARGS_CODE
        # Add arguments to the parser
        args = parser.parse_args(argv)
        # Do something with the arguments
        return 0

    if __name__ == "__main__":
        sys.exit(main(sys.argv))
    ```
    
    """
    cmd = "?"
    if len(argv) >= 3:
        cmd = argv[2]
    parser = argparse.ArgumentParser(prog=cmd, description=description)

    if len(argv) < 3 or not os.path.isfile(argv[1]):
        print("Must be run from the scoter script.")
        return (None, [], parser)

    with open(argv[1], "rb") as inp:
        config = UserConfig(json.load(inp))

    parser.add_argument(
        "-l",
        action=LogAction,
        help="Select logging level.",
    )
    return (config, argv[3:], parser)


class LogAction(argparse.Action):
    """Handle a log argument group."""

    def __init__(self, option_strings: Sequence[str], dest: str, help: str) -> None:
        super().__init__(
            ["--verbose", "--debug", "--trace"],
            dest, nargs=0, const=None, default=None, type=None,
            choices=None, required=False, help=help, metavar=None, deprecated=False
        )

    def format_usage(self) -> str:
        """The usage text: each option, separated by `|`."""
        return ' | '.join(self.option_strings)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        if option_string == "--verbose":
            log.set_level(log.INFO)
        elif option_string == "--debug":
            log.set_level(log.USER_DEBUG)
        elif option_string == "--trace":
            log.set_level(log.CONTRIB_DEBUG)
        else:
            log.set_level(log.WARNING)
