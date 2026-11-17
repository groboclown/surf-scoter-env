#SPDX:MIT
"""Undo `scoter user-setup` (not implemented yet).

A Surf Scoter sub-command.

Meant for uninstalling: it's to remove what `scoter user-setup` added to
your user directories (the ssh key pair it made, and your
`~/.config/surf-scoter.d` setup), rather than what runs leave behind,
which is `scoter cleanup`'s job.  For now it does nothing, and fails.
"""
import sys
from collections.abc import Sequence

from surf_scoter_lib import BAD_EXECUTION_ARGS_CODE, UserConfig, gen_parser, log


def run_cleanup_user(config: UserConfig) -> int:
    """Undo `scoter user-setup`; not implemented yet."""
    log.error(
        "`scoter user-cleanup` isn't implemented yet.  For what runs left behind, use "
        "`scoter cleanup`; to uninstall, see docs/cleaning-up.md."
    )
    return 1


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "Undo `scoter user-setup`, for uninstalling: remove what it added to your user "
        "directories.  Not implemented yet: it does nothing, and fails.  For what runs, "
        "pulls and deleted projects left behind, use `scoter cleanup`."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE

    _args = parser.parse_args(argv)
    return run_cleanup_user(config)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
