#SPDX:MIT
"""General categories of error."""

class SurfScoterErr(Exception):
    """Category for all custom surf scoter errors."""


class ConfigErr(SurfScoterErr):
    """A configuration problem happened."""


class SshConfigErr(ConfigErr):
    """The ssh config file has an issue."""


class ActiveEnvErr(ConfigErr):
    """The active environment file has an issue."""


class VerifiedStoreErr(ConfigErr):
    """The verified (accepted) project settings store has a problem."""


class LockTimeoutErr(SurfScoterErr):
    """Another command held a lock for too long."""
