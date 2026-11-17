#SPDX:MIT
"""What the setup says about reading a configuration's files: the limits, and the build's directories."""
from pathlib import Path

from ..user_config import UserConfig
from . import snapshot


def limits(config: UserConfig) -> snapshot.SnapshotLimits:
    """The snapshot limits, from the setup's `verification: max-*` values."""
    defaults = snapshot.SnapshotLimits()
    return snapshot.SnapshotLimits(
        max_file_size=config.optional_int_value("verification", "max-file-size")
        or defaults.max_file_size,
        max_snapshot_size=config.optional_int_value("verification", "max-snapshot-size")
        or defaults.max_snapshot_size,
    )


def container_dirs(config: UserConfig) -> list[Path]:
    """The container directories the dev container builds from."""
    parents = (
        [directory for _, directory in config.dev_container_parent_builds()]
        if config.has_dev_container() else []
    )
    return snapshot.container_dirs(config.active_location("dev-container-path"), parents)
