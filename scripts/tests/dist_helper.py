#SPDX:MIT
"""A scratch distribution and project, for tests that run the generator.

The tests using it need the debug generator (`cd host && cargo build`), and
skip without it.
"""
import json
import os
import shutil
import subprocess  # nosec: B404  # Runs the generator to make the tests' universal files.
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).parents[1]
REPO = SCRIPTS.parent
GENERATOR = REPO / "host" / "target" / "debug" / "gen-scoter-uc"
POSIX = sys.platform != "win32"
HAVE_GENERATOR = POSIX and GENERATOR.is_file()
NEEDS_GENERATOR = "needs the generator (cd host && cargo build)"

PROJECT_FILE = """\
container:
  dev-container-path: ./env
egress:
  - {host: registry.npmjs.org, protocol: tls}
commands:
  - {command: ls, args: [{any: {}}]}
  - {command: sh, args: ["-c", {any: {}}]}
"""


class DistTest(unittest.TestCase):
    """A distribution like an installed one, a user setup and a project.

    * `self.dist`: the generator, the scripts, the defaults and containers;
    * `self.home`: the user's setup, with `verified-dir` at `self.verified`,
      and `self.mine` (the user's own containers) searched after the
      project's files directory;
    * `self.project`: a project with `PROJECT_FILE` and an `env` container.
    """

    def setUp(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.tmp = Path(os.path.realpath(directory))
        os.chmod(self.tmp, 0o700)
        self.dist = self.tmp / "dist"
        (self.dist / "bin").mkdir(parents=True)
        os.symlink(GENERATOR, self.dist / "bin" / "gen-scoter-uc")
        for script in SCRIPTS.glob("*.py"):
            os.symlink(script, self.dist / "bin" / script.name)
        os.symlink(SCRIPTS / "surf_scoter_lib", self.dist / "bin" / "surf_scoter_lib")
        os.symlink(REPO / "defaults", self.dist / "defaults")
        os.symlink(REPO / "containers", self.dist / "containers")
        shutil.copy(REPO / "defaults" / "setup.yaml", self.dist / "setup.yaml")
        self.verified = self.tmp / "verified"
        self.mine = self.tmp / "mine"
        self.mine.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.home / "setup.yaml").write_text(
            "scoter:\n"
            f"  paths:\n    verified-dir: {json.dumps(str(self.verified))}\n"
            "  path-lists:\n    dev-containers-search:\n"
            "      - ${scoter:project-dir}/${scoter:project-files-dir}\n"
            f"      - {json.dumps(str(self.mine))}\n",
            encoding="utf-8",
        )
        self.project = self.tmp / "proj"
        self.files = self.project / ".surf-scoter.d"
        (self.files / "env").mkdir(parents=True)
        (self.files / "project.yaml").write_text(PROJECT_FILE, encoding="utf-8")
        (self.files / "env" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        self.dev_env_yaml = (REPO / "containers" / "dev-env" / "dev-env.yaml").read_text(encoding="utf-8")
        (self.files / "env" / "dev-env.yaml").write_text(self.dev_env_yaml, encoding="utf-8")
        self.generate("--distribution-dir", str(self.dist), output=self.tmp / "first.json")

    def generate(self, *args: str, output: Path | None = None) -> dict:
        """Run the generator with `args`, as the user; its universal file."""
        output = output or Path(tempfile.mkdtemp(dir=self.tmp)) / "suc.json"
        subprocess.run(  # nosec: B603  # The generator under test.
            [str(GENERATOR), *args, "--output", str(output)],
            env={**os.environ, "SURF_SCOTER_HOME": str(self.home)},
            check=True, capture_output=True,
        )
        return json.loads(output.read_text(encoding="utf-8"))

    def project_suc(self, accepted: Path | None = None) -> dict:
        """The universal file with the project (`scoter`'s second run), or with `accepted` (its third)."""
        args = ["--universal", str(self.tmp / "first.json"), "--no-setup-search",
                "--project-dir", str(self.project)]
        if accepted is not None:
            args.extend(("--accepted-settings", str(accepted)))
        return self.generate(*args)
