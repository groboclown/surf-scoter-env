#SPDX:MIT
"""Common output handling routines."""
import sys
from typing import Literal

LogLevel = Literal[0, 1, 2, 3, 4, 5, 6, 7]

FATAL: LogLevel = 0
NOTICE: LogLevel = 1
ERROR: LogLevel = 2
WARNING: LogLevel = 3
INFO: LogLevel = 4
USER_DEBUG: LogLevel = 6
CONTRIB_DEBUG: LogLevel = 7


LEVEL: list[LogLevel] = [WARNING]


def set_level(level: LogLevel) -> LogLevel:
    """Set the current log level, and return the old."""
    old = LEVEL[0]
    LEVEL[0] = level
    return old


def error(text: str) -> None:
    """Output an error."""
    if LEVEL[0] >= ERROR:
        print(text, file=sys.stderr)


def warning(text: str) -> None:
    """Output a warning."""
    if LEVEL[0] >= WARNING:
        print(text, file=sys.stderr)


def contrib(text: str) -> None:
    """Output for contributors."""
    if LEVEL[0] >= CONTRIB_DEBUG:
        print(f"[CONTRIB] {text}", file=sys.stderr)
