#SPDX:MIT
"""Run the distribution's universal configuration file generator, `gen-scoter-uc`.

For the scripts that need the generator's view of a file they have: a
review file (`main_verify.py`), or a staged copy of the project's files
(`main_run.py`).  Each run starts from the universal configuration file the
script was given, without looking for the setup files again.
"""
import json
import os
import subprocess  # nosec: B404  # Runs the distribution's own generator.
import tempfile
from collections.abc import Sequence
from pathlib import Path

from . import jdata
from .defaults import GEN_SCOTER_UC_EXEC
from .user_config import UserConfig

TIMEOUT_SECONDS = 60


def run(
    config: UserConfig, args: Sequence[str], directory: Path,
) -> tuple[jdata.DictJsonData | None, list[str]]:
    """The generator's universal file, from `config` with `args`; or its complaints.

    `directory` must be private to the user: `config` is written there for
    the generator to start from, and its output read back, and both are
    removed afterwards.
    """
    fd, universal = tempfile.mkstemp(suffix="-universal.json", dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(json.dumps(config.raw()))
    output = Path(tempfile.mkdtemp(dir=directory)) / "generated.json"
    command = [
        str(config.get_dist_bin_dir() / GEN_SCOTER_UC_EXEC),
        "--universal", universal, "--no-setup-search", *args, "--output", str(output),
    ]
    try:
        res = subprocess.run(  # nosec: B603  # The distribution's own generator.
            command, capture_output=True, check=False, shell=False, timeout=TIMEOUT_SECONDS,
        )
        if res.returncode != 0:
            lines = res.stderr.decode("utf-8", errors="replace").splitlines()
            return None, [line for line in lines if line.strip()] or ["the generator failed"]
        with open(output, "rb") as inp:
            data = json.load(inp)
    except (OSError, ValueError, subprocess.SubprocessError) as err:
        return None, [f"couldn't run the generator: {err}"]
    finally:
        os.unlink(universal)
        if os.path.lexists(output):
            os.unlink(output)
        os.rmdir(output.parent)
    if not isinstance(data, dict):
        return None, ["the generator wrote something unexpected"]
    return data, []
