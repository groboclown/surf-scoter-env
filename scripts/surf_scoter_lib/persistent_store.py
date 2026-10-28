#SPDX:MIT
"""The dev container's persistent store: a container volume kept between runs.

It's mounted at the dev container's `mounts: persistent-store`, and named by
the setup's `project-state: values: persistent-store-volume` (by default, from
the project's id).  It isn't a host directory, so host commands can't reach
it.  `scoter run` creates it; `--clean-persistent-store` on `run` and `kill`
removes it; `scoter cleanup --persistent-stores` finds those no running
environment uses.
"""
import re

from .err import ConfigErr
from .user_config import OnError, UserConfig

# A container volume name.
VOLUME_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")


def volume_name(config: UserConfig) -> str:
    """The name of the project's persistent store volume, checked."""
    volume = config.require_value("project-state", "persistent-store-volume")
    if not VOLUME_NAME.fullmatch(volume):
        raise ConfigErr(
            f"The setup's `project-state` `values: persistent-store-volume` ({volume!r}) "
            "must be a volume name: lower-case letters, digits, `_`, `.` and `-`, "
            "starting with a letter or digit."
        )
    return volume


def create(config: UserConfig, volume: str) -> None:
    """Create the volume `volume`; it's fine if it's already there."""
    config.run_command("docker", "create-volume", template={"name": volume})


def remove(config: UserConfig, volume: str) -> bool:
    """Remove the volume `volume`; whether that worked (it may not have been there)."""
    print(f"Removing the persistent store ({volume})")
    res = config.run_command(
        "docker", "remove-volume", template={"name": volume}, on_error=OnError.RETURN,
    )
    return res.returncode == 0
