#SPDX:MIT
"""Test the setup_check module."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

from ...surf_scoter_lib import jdata, setup_check, user_config


def _config(
    programs: dict[str, str],
    found: dict[str, str | None] | None = None,
    locations: dict[str, str] | None = None,
) -> user_config.UserConfig:
    """A universal configuration whose one setup file has these `t` programs."""
    resolved_programs: jdata.DictJsonData = dict(found or {})
    # As the generator writes it: every location, null when it isn't set.
    active: jdata.DictJsonData = {
        name: None
        for name in (*setup_check.PROJECT_LOCATIONS, "user-dir", "distribution-dir")
    }
    active.update(locations or {})
    data: jdata.DictJsonData = {
        "active-config-locations": active,
        "setup": {
            "files": [
                {
                    "file": "/home/me/.config/surf-scoter.d/setup.yaml",
                    "source": "user",
                    "sections": {"t": {"programs": dict(programs)}},
                }
            ]
        },
        "resolved": {"t": {"programs": resolved_programs}},
    }
    return user_config.UserConfig(data)


class CheckProgramsTest(unittest.TestCase):
    """Test the check_programs function."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(os.path.realpath(self._tmp.name))
        self.home = self.dir / "me"
        self.home.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def check(self, config: user_config.UserConfig, path_env: str = "/usr/bin") -> list[str]:
        """The problems' keys and the start of their reasons."""
        return [
            f"{problem.key}: {problem.reason.split(',')[0].split(';')[0]}"
            for problem in setup_check.check_programs(config, path_env)
        ]

    def test_fine(self) -> None:
        """Bare names, absolute paths, and paths from the home directory are fine."""
        config = _config(
            {"a": "python3", "b": "/usr/bin/git", "c": "~/bin/tool", "d": "${other:exec}"},
            {"a": "/usr/bin/python3", "b": "/usr/bin/git", "c": str(self.home / "bin/tool")},
        )
        self.assertEqual(self.check(config), [])

    def test_written_relative_to_the_project(self) -> None:
        """A `./` value, or a reference to a project location, is reported."""
        config = _config({
            "a": "./venv/bin/python",
            "b": ".",
            "c": "${scoter:project-dir}/tool",
            "d": "${scoter:cwd}/tool",
            "e": "bin/tool",
        })
        self.assertEqual(self.check(config), [
            "t.programs.a: starts with './'",
            "t.programs.b: starts with './'",
            "t.programs.c: refers to `${scoter:project-dir}`",
            "t.programs.d: refers to `${scoter:cwd}`",
            "t.programs.e: is a relative path",
        ])

    def test_found_inside_the_project(self) -> None:
        """A program found inside the project, or its repository, is reported."""
        (self.dir / "repo/client/.surf-scoter.d").mkdir(parents=True)
        locations = {
            "vcs-root": str(self.dir / "repo"),
            "project-dir": str(self.dir / "repo/client"),
            "project-config": str(self.dir / "repo/client/.surf-scoter.d/project.yaml"),
            "dev-container-workspace-dir": str(self.dir / "ws"),
        }
        config = _config(
            {"a": "${x:a}", "b": "${x:b}", "c": "${x:c}", "d": "${x:d}", "e": "/usr/bin/ok"},
            {
                "a": str(self.dir / "repo/client/tool"),
                "b": str(self.dir / "repo/server/tool"),
                "c": str(self.dir / "ws/tool"),
                # Written another way, it's still in the project.
                "d": str(self.dir / "elsewhere/../repo/client/.surf-scoter.d/tool"),
                "e": "/usr/bin/ok",
            },
            locations,
        )
        self.assertEqual(self.check(config), [
            "t.programs.a: is inside the project (the project directory)",
            "t.programs.b: is inside the project (the repository)",
            "t.programs.c: is inside the project (the workspace)",
            "t.programs.d: is inside the project (the project directory)",
        ])

    def test_relative_path_entries(self) -> None:
        """An empty or relative PATH entry is reported."""
        path_env = os.pathsep.join(["/usr/bin", "", "bin", "/opt/x"])
        self.assertEqual(self.check(_config({}), path_env), [
            "PATH: is relative",
            "PATH: is relative",
        ])


@unittest.skipIf(sys.platform == "win32", "modes")
class ProgramFindingsTest(unittest.TestCase):
    """Test program_findings: the setup's programs as found, as `scoter run` checks them."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(os.path.realpath(self._tmp.name))
        os.chmod(self.dir, 0o755)  # nosec: B103  # The usual modes, so only the test's are found.
        self.bin = self.dir / "bin"
        self.bin.mkdir(mode=0o755)
        os.chmod(self.bin, 0o755)  # nosec: B103  # As above.

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def tool(self, name: str, directory: Path | None = None) -> str:
        path = (directory or self.bin) / name
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(path, 0o755)  # nosec: B103  # As above.
        return str(path)

    def findings(
        self, config: user_config.UserConfig, path_env: str = "/usr/bin",
    ) -> list[tuple[str, str, str]]:
        """Each finding about this test's programs (the machine's own directories aside)."""
        return [
            (str(f.severity), f.key, f.message)
            for f in setup_check.program_findings(config, path_env)
            if f.key == "PATH" or str(self.dir) in f.message or not f.message.startswith(("Other", "Anyone"))
        ]

    def test_fine(self) -> None:
        config = _config({"a": "${x:a}"}, {"a": self.tool("a")})
        self.assertEqual(self.findings(config), [])

    def test_check_programs_refuse_and_path_warns(self) -> None:
        """`check_programs`' problems refuse; a relative PATH entry only warns."""
        config = _config({"a": "./venv/bin/python"})
        found = self.findings(config, os.pathsep.join(["/usr/bin", "bin"]))
        self.assertEqual([(s, k) for s, k, _ in found], [("refuse", "t.programs.a"), ("warn", "PATH")])
        self.assertIn("/home/me/.config/surf-scoter.d/setup.yaml: './venv/bin/python' starts with './'", found[0][2])

    def test_writable_by_others(self) -> None:
        """A program anyone can write to refuses; one its group can, warns."""
        anyone = self.tool("anyone")
        os.chmod(anyone, 0o757)  # nosec: B103  # What the test checks for.
        group = self.tool("group")
        os.chmod(group, 0o775)  # nosec: B103  # What the test checks for.
        config = _config({"a": "${x:a}", "b": "${x:b}"}, {"a": anyone, "b": group})
        found = self.findings(config)
        self.assertIn(("refuse", "t.programs.a", f"Anyone can write to `a`'s file {anyone}, and change what runs."), found)
        self.assertIn(("warn", "t.programs.b", f"Other members of its group can write to `b`'s file {group}."), found)

    def test_version_manager_shim(self) -> None:
        """A setup program that's a shim is refused, as a project command's would be."""
        shims = self.dir / "shims"
        shims.mkdir(mode=0o755)
        os.chmod(shims, 0o755)  # nosec: B103  # As above.
        config = _config({"git": "${x:git}"}, {"git": self.tool("git", shims)})
        config.raw()["host-command-checks"] = {"checks": {
            "verification:version-manager-shims": {
                "action": "refuse", "reason": "A shim picks the real program.", "paths": [str(shims)],
                "files": [], "commands": {}, "name-patterns": [], "options": [], "env": [], "overrides": [],
            },
            # Checks by name don't apply to the setup's programs.
            "verification:vcs": {
                "action": "refuse", "reason": "Not for the setup.", "paths": [],
                "files": [], "commands": {"git": None}, "name-patterns": ["git"], "options": [], "env": [],
                "overrides": [],
            },
        }, "command-map": {}}
        self.assertEqual(self.findings(config), [
            ("refuse", "t.programs.git", "A shim picks the real program."),
        ])


class HostCommandReportTest(unittest.TestCase):
    """Test host_command_report."""

    def test_report(self) -> None:
        """The generator's notices, then each override in effect."""
        config = user_config.UserConfig({
            "host-command-checks": {
                "checks": {
                    "verification:shells": {
                        "action": "refuse", "commands": {"sh": "/bin/sh"}, "overrides": [],
                    },
                    "mine:ksh": {
                        "action": "warn",
                        "commands": {"ksh": "/bin/ksh", "dash": None},
                        "overrides": ["verification:shells"],
                    },
                },
                "notices": [{
                    "message": "/u/setup.yaml removes `a:b`",
                    "id": "check-removed",
                    "parameters": {"check": "a:b"},
                    "sources": ["/u/setup.yaml", "/d/setup.yaml"],
                }],
            },
        })
        self.assertEqual(
            setup_check.host_command_report(config),
            [
                "/u/setup.yaml removes `a:b`",
                "`mine:ksh` overrides `verification:shells` for `dash`, `ksh`, with `warn`",
            ],
        )
        self.assertEqual(len(config.host_command_checks()), 2)

    def test_no_checks(self) -> None:
        """An older universal file has no checks."""
        config = user_config.UserConfig({})
        self.assertEqual(setup_check.host_command_report(config), [])
        self.assertEqual(config.host_command_checks(), {})

    def test_bad_notices(self) -> None:
        """A notice whose message isn't text is a bad file."""
        config = user_config.UserConfig({"host-command-checks": {"notices": [{"message": 5}]}})
        with self.assertRaises(user_config.ConfigErr):
            setup_check.host_command_report(config)
