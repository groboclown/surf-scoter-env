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


class ProjectAuthorizationFormatErr(ConfigErr):
    """The authorized project format file had an error."""
