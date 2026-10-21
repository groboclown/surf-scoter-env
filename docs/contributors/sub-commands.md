# Creating and Maintaining Sub-Commands

The Surf Scoter main script works a bit like other sub-command tools, where it hands sub-command processing to specially constructed child programs, where they must conform to certain standards.

1. The sub-commands run as Python scripts, with the interpreter the system setup names: the `python` section's `runs: sub-command` (by default, `${exec} -E -s {x:script}`, with `programs: exec` normally `python3`).  That needn't be the interpreter that runs the main script; it can be a virtual environment's, if a sub-command needs installed libraries.  `-E` and `-s` mean the scripts don't see `PYTHON*` environment variables or the user's site-packages, so import only from the standard library and the `bin` directory (such as `surf_scoter_lib`), unless the interpreter is a virtual environment's.
2. The main script searches for the main script using this algorithm:
    1. convert the sub-command name to the filename:
        1. convert `-` characters to `_`,
        2. prepend `main_`,
        3. append `.py`;
    2. look in the distribution's `bin` directory for a file with that name.
3. The main script hands the sub-command script the arguments:
    1. the absolute path to the invoked script;
    2. the path to the execution's [SUC file](universal-configuration-file.md);
    3. the sub-command name;
    4. the remaining arguments passed to the sub-command.

To help with this, the `bin` directory contains the library [`surf_scoter_lib`](../../scripts/surf_scoter_lib/__init__.py) to provide many helpful tools.

This makes the general boilerplate for a sub-command:

```python
#SPDX:your-license
"""Surf Scoter Sub-command: my-command"""
# That doc string is just convention.  Nothing requires it.
from collections.abc import Sequence
import sys
from surf_scoter_lib import log, gen_parser, BAD_EXECUTION_ARGS_CODE

def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, "Example of a command structure.")
    if not config:
        return BAD_EXECUTION_ARGS_CODE
    # The above command takes the CLI arguments + the description of the
    # sub-command (for help purposes), and returns:
    #   config - a UserConfig object.
    #   argv -   the remaining arguments (sequence of strings) for the
    #            sub-command, which you can pass to the `parser` (see below).
    #   parser - an argparse.ArgumentParser object that contains common setup
    #            arguments around logging.  You should add in additional
    #            arguments to this.

    # Add arguments to the parser
    parser.add_argument("-f", action="store_true", help="force")

    # Parse the arguments.
    args = parser.parse_args(argv)

    # At this point, logging is configured for use.
    if args.f:
        log.notice("Starting work.")
    else:
        log.info("Executing dry run.")

    # Do something with the arguments

    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

