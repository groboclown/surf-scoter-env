#SPDX:MIT
"""Unit tests for the Python sources.

The installed scripts import the library as their own directory's (they run
from the distribution's `bin` directory), and `scoter` runs on its own from
the distribution's top directory.  So both source directories go on the path
here, before any test module imports them.
"""
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
PY = TESTS.parent
REPO = PY.parent
# The sources installed into the distribution's `bin` directory.
SRC_BIN = PY / "src" / "dist_bin"
# The sources installed into the distribution's top directory.
SRC_ROOT = PY / "src" / "dist_root"

for _path in (SRC_ROOT, SRC_BIN):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
