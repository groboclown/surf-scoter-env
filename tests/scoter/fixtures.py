#SPDX:MIT
"""Unit test fixtures for the 'scoter.py' script."""

__all__ = (
    "scoter_lib",
    "not_none",
)

import os

import importlib.machinery
import importlib.util


def not_none[T](val: T | None) -> T:
    """Ensure the value is not None."""
    if val is None:
        raise AssertionError("none argument")
    return val


# Import the scoter script as a module.
loader = importlib.machinery.SourceFileLoader(
    "scoter_lib",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "scoter.py",
    )
)
spec = importlib.util.spec_from_loader('scoter_lib', loader)
scoter_lib = importlib.util.module_from_spec(not_none(spec))
loader.exec_module(scoter_lib)
