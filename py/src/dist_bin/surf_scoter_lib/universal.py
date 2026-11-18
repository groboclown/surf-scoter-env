#SPDX:MIT
"""Reading the universal configuration file (the SUC) that `scoter` hands to its scripts.

Every script that `scoter` (or the setup) runs gets the universal
configuration file's path as its first argument.  This reads it the same
way for all of them: a bounded read, so a huge or endless file can't use up
the memory, and a check that it's a JSON object, before it becomes a
`UserConfig`.
"""
import json
import os

from .err import SurfScoterErr
from .user_config import UserConfig

# The largest universal configuration file read.  Real ones are a few
# hundred kilobytes, at most.
MAX_UNIVERSAL_BYTES = 4 * 1024 * 1024


class UniversalErr(SurfScoterErr):
    """The universal configuration file can't be read, or isn't one."""


def read_universal(path: str | os.PathLike[str]) -> UserConfig:
    """The universal configuration file at `path`, checked: no larger than
    `MAX_UNIVERSAL_BYTES`, and a JSON object.  Raises `UniversalErr`, saying
    which file and why, but never quoting it."""
    try:
        with open(path, "rb") as inp:
            data = inp.read(MAX_UNIVERSAL_BYTES + 1)
    except OSError as err:
        raise UniversalErr(f"{path}: can't read it: {err.strerror or err}") from err
    if len(data) > MAX_UNIVERSAL_BYTES:
        raise UniversalErr(f"{path}: larger than {MAX_UNIVERSAL_BYTES} bytes")
    try:
        suc = json.loads(data)
    except ValueError:
        # The parser's message can quote the file.
        raise UniversalErr(f"{path}: not JSON") from None
    if not isinstance(suc, dict):
        raise UniversalErr(f"{path}: not a universal configuration file")
    return UserConfig(suc)
