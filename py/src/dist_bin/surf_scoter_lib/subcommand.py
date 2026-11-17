#SPDX:MIT
"""Run one sub-command from another, the way `scoter` runs them.

`scoter` builds the universal configuration file (the SUC), saves it to a
private temporary file, and runs `main_NAME.py` with it through the setup's
`python: runs: sub-command`.  A porcelain sub-command (`pull`, `lock`,
`review`) runs its phases the same way, with one difference: it hands on the
SUC it was given rather than building a new one, so the porcelain and its
phases always agree on where the state directory and the store are
(VER-29).
"""
import contextlib
import json
import os
import shutil
import subprocess  # nosec: B404  # Runs the distribution's own sub-commands.
import tempfile
from collections.abc import Generator, Sequence
from dataclasses import dataclass

from .err import ConfigErr
from .user_config import UserConfig

SUB_CMD_PREFIX = "main_"
SUB_CMD_SUFFIX = ".py"
# The exit code for a sub-command that couldn't start, as shells use it.
EXIT_CANNOT_START = 127


@dataclass(frozen=True)
class Finished:
    """A sub-command's exit code, and its output when it was captured."""

    code: int
    stdout: bytes | None = None


def command(config: UserConfig, name: str, suc_file: str) -> list[str]:
    """The command line that runs the sub-command `name` with the SUC in `suc_file`.

    As `scoter.py`'s: the setup's `python: runs: sub-command`, with its
    `{x:script}` the distribution's `main_NAME.py`, then the SUC file and
    the sub-command name.
    """
    script = config.get_dist_bin_dir() / (SUB_CMD_PREFIX + name.replace("-", "_") + SUB_CMD_SUFFIX)
    if not script.is_file():
        raise ConfigErr(f"{script}: no such sub-command")
    prefix = config.require_run("python", "sub-command", {"script": str(script)}, whole_args=True)
    if not prefix or not os.path.isabs(prefix[0]):
        raise ConfigErr(
            "The setup's `python: runs: sub-command` must start with a program, found as an "
            "absolute path."
        )
    return [*prefix, suc_file, name]


@contextlib.contextmanager
def suc_file(config: UserConfig) -> Generator[str]:
    """`config` saved as a SUC file for another program, removed afterwards.

    It goes in a new directory only the user can use, as a file only they
    can read, as `scoter` saves it: the SUC decides what runs on the host.
    """
    directory = tempfile.mkdtemp(prefix="surf-scoter-")
    try:
        fd, path = tempfile.mkstemp(dir=directory, suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(config.raw(), out)
        yield path
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def run(
    config: UserConfig, name: str, args: Sequence[str] = (), *, capture: bool = False,
) -> Finished:
    """Run the sub-command `name` with `args`, handing on `config` as its SUC.

    The SUC is saved by `suc_file`, and removed however the sub-command
    ends.  With `capture`, its standard output is returned; otherwise it
    goes to the user, as does its standard error.
    """
    with suc_file(config) as path:
        cmd = [*command(config, name, path), *args]
        try:
            res = subprocess.run(  # nosec: B603  # The distribution's own sub-command, as a list.
                cmd, stdout=subprocess.PIPE if capture else None, check=False, shell=False,
            )
        except OSError as err:
            raise ConfigErr(f"couldn't start the `{name}` sub-command: {err}") from err
    return Finished(res.returncode, res.stdout if capture else None)
