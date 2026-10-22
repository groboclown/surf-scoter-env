#SPDX:MIT
"""Test the checks module."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from ...surf_scoter_lib import checks, fileutil, jdata
from ...surf_scoter_lib.user_config import UserConfig

POSIX = not fileutil.is_windows()
Severity = checks.Severity


class _Project(unittest.TestCase):
    """A project, a `bin` directory of programs, and a universal configuration file for them."""

    def setUp(self) -> None:
        self._dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._dir, True)
        self.tmp = Path(os.path.realpath(self._dir))
        os.chmod(self.tmp, 0o700)
        self.project = self.tmp / "proj"
        self.write("proj/.surf-scoter.d/project.yaml", "x: 1\n")
        self.write("proj/.surf-scoter.d/env/Dockerfile", "FROM scratch\n")
        self.bin = self.tmp / "bin"
        self.bin.mkdir(mode=0o700)
        self.path_env = str(self.bin)
        self.commands: list[jdata.JsonData] = []
        self.host_checks: dict[str, jdata.JsonData] = {}
        self.command_map: dict[str, jdata.JsonData] = {}
        self.egress: list[jdata.JsonData] = []
        self.project_files: dict[str, jdata.JsonData] = {}
        self.egress_hosts: dict[str, jdata.JsonData] = {}

    def write(self, rel: str, text: str, mode: int = 0o644) -> Path:
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)
        return path

    def tool(self, name: str, where: Path | None = None) -> Path:
        directory = where or self.bin
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(path, 0o700)
        return path

    def suc(self) -> jdata.DictJsonData:
        return {
            "active-config-locations": {
                "project-dir": str(self.project),
                "vcs-root": str(self.project),
                "dev-container-path": str(self.project / ".surf-scoter.d" / "env"),
            },
            "resolved": {
                "scoter": {
                    "values": {"project-files-dir": ".surf-scoter.d", "project-file-name": "project.yaml"},
                },
                "verification": {"project-files": self.project_files, "egress-hosts": self.egress_hosts},
            },
            "current-project-setup": {"commands": self.commands, "egress": self.egress},
            "host-command-checks": {"checks": self.host_checks, "command-map": self.command_map},
            "dev-container": {"parent-builds": []},
        }

    def check(self, name: str, action: str, **fields: jdata.JsonData) -> None:
        """A combined check, with every field, as the generator writes them."""
        self.host_checks[name] = {
            "files": [], "action": action, "reason": f"because {name}",
            "commands": {}, "name-patterns": [], "options": [], "env": [], "paths": [],
            "overrides": [],
            **fields,
        }

    def report(self) -> checks.Report:
        return checks.check_project(UserConfig(self.suc()), self.path_env)

    def host_findings(self) -> list[checks.Finding]:
        """Only the `host-commands` checks' findings (the temporary directories' own
        modes vary from computer to computer)."""
        config = UserConfig(self.suc())
        return checks.check_host_commands(config, checks.resolve_programs(config, self.path_env))

    def host_found(self, severity: Severity | None = None) -> list[tuple[str, str, str]]:
        return [
            (str(f.severity), f.key, f.message)
            for f in self.host_findings()
            if severity is None or f.severity == severity
        ]

    def found(self, severity: Severity | None = None) -> list[tuple[str, str, str]]:
        return [
            (str(f.severity), f.key, f.message)
            for f in self.report().findings
            if severity is None or f.severity == severity
        ]


class ResolveTest(_Project):
    """Test finding the project's programs."""

    def test_command_map_then_path(self) -> None:
        """The setup's `command-map` first; else the PATH's absolute entries."""
        aws = self.tool("aws")
        podman = self.tool("podman")
        self.command_map["docker"] = {"program": str(podman), "checks": ["docker:docker"]}
        self.commands = [
            {"command": "aws", "args": []},
            {"command": "d", "program": "docker", "args": []},
            {"command": "nope", "args": []},
        ]
        resolved = checks.resolve_programs(UserConfig(self.suc()), self.path_env)
        self.assertEqual(
            [(r.name, r.executable, r.source) for r in resolved],
            [("aws", str(aws), "path"), ("docker", str(podman), "command-map"), ("nope", None, "path")],
        )
        self.assertIn(
            ("refuse", "commands[2].program", "`nope` isn't on this computer"),
            self.found(),
        )
        # A relative PATH entry is skipped, and noted.
        self.assertIsNone(checks.find_on_path("aws", "bin"))
        self.path_env = os.pathsep.join(["bin", str(self.bin)])
        self.assertEqual(self.found(Severity.WARN)[0][1], "PATH")


@unittest.skipUnless(POSIX, "symlinks and modes")
class HostCommandsTest(_Project):
    """Test the setup's `host-commands` checks against the project's commands."""

    def test_through_a_link(self) -> None:
        """`mytool -> env` is refused as `env` is."""
        env = self.tool("env", self.tmp / "usr" / "bin")
        os.symlink(env, self.bin / "mytool")
        self.check("verification:command-runners", "refuse", commands={"env": str(env)})
        self.commands = [{"command": "mytool", "args": []}]
        self.assertEqual(
            self.host_found(Severity.REFUSE),
            [("refuse", "commands[0].program", "because verification:command-runners")],
        )

    def test_name_patterns(self) -> None:
        """A pattern matches the name, or the base name of the file it really is."""
        python = self.tool("python3.12")
        os.symlink(python, self.bin / "py")
        self.check("verification:interpreters", "refuse", **{"name-patterns": ["python*"]})
        self.commands = [{"command": "py", "args": []}]
        self.assertEqual(len(self.host_found(Severity.REFUSE)), 1)

    def test_shim(self) -> None:
        """A program in a version manager's shims directory."""
        shims = self.tmp / "home" / ".asdf" / "shims"
        self.tool("node", shims)
        self.path_env = str(shims)
        self.check("verification:version-manager-shims", "refuse", paths=[str(shims)])
        self.commands = [{"command": "node", "args": []}]
        self.assertEqual(
            self.host_found(Severity.REFUSE),
            [("refuse", "commands[0].program", "because verification:version-manager-shims")],
        )

    def test_unknown_and_allowed(self) -> None:
        """No check: a note.  An `allow` check: a note, with its reason."""
        self.tool("aws")
        self.commands = [{"command": "aws", "args": []}]
        notes = self.host_found(Severity.NOTE)
        self.assertEqual(len(notes), 1)
        self.assertIn("No check in your setup covers `aws`", notes[0][2])
        self.check("mine:aws", "allow", commands={"aws": str(self.bin / "aws")})
        self.assertEqual(self.host_found(Severity.NOTE), [("note", "commands[0].program", "because mine:aws")])

    def test_overrides(self) -> None:
        """Most restrictive first, then the override replaces the check it names."""
        ksh = self.tool("ksh")
        self.check("verification:shells", "refuse", commands={"ksh": str(ksh)})
        self.check("verification:korn", "warn", commands={"ksh": str(ksh)})
        self.check(
            "mine:ksh", "allow", commands={"ksh": str(ksh)}, overrides=["verification:shells"],
        )
        self.commands = [{"command": "ksh", "args": []}]
        found = self.host_findings()
        self.assertEqual(
            sorted((str(f.severity), f.checks) for f in found),
            [
                ("note", ("mine:ksh",)),
                ("note", ("verification:shells", "mine:ksh")),
                ("warn", ("verification:korn",)),
            ],
        )

    def test_options(self) -> None:
        """An option that runs code, with a value the container fills in."""
        git = self.tool("git")
        self.check("git-vcs:git-options", "warn", commands={"git": str(git)}, options=["-c"])
        self.check("verification:code-options", "warn", options=["--eval"])
        self.commands = [
            {"command": "git", "args": ["-c", {"any": {}}, "status"]},
            {"command": "git", "args": ["-c", "core.pager=cat", "log"]},
            {"command": "git", "args": [{"any": {"prefix": "--eval="}}]},
        ]
        warns = [(f.key, f.checks) for f in self.host_findings() if f.severity == Severity.WARN]
        self.assertEqual(
            warns,
            [
                ("commands[0].args[1]", ("git-vcs:git-options",)),
                ("commands[2].args[0]", ("verification:code-options",)),
            ],
        )

    def test_env(self) -> None:
        """`env` entries match the `allow` entries exactly, the project's and the command's."""
        self.tool("aws")
        self.check("verification:loader-env", "refuse", env=["LD_PRELOAD", "*"])
        self.commands = [{"command": "aws", "args": [], "env": {"allow": ["AWS_*", "*"]}}]
        suc = self.suc()
        suc["current-project-setup"]["env"] = {"allow": ["LD_PRELOAD"]}  # type: ignore[index]
        config = UserConfig(suc)
        found = [f for f in checks.check_host_commands(config, checks.resolve_programs(config, self.path_env)) if f.severity == Severity.REFUSE]
        self.assertEqual(sorted(f.key for f in found), ["commands[0].env.allow", "env.allow"])


@unittest.skipUnless(POSIX, "modes")
class ProgramPathsTest(_Project):
    """Test where the programs are."""

    def test_others_can_write(self) -> None:
        """A program in a directory anyone can write to is refused; group-writable warns."""
        shared = self.tmp / "shared"
        self.tool("aws", shared)
        os.chmod(shared, 0o777)  # nosec: B103  # Testing a directory others can write to.
        self.path_env = str(shared)
        self.commands = [{"command": "aws", "args": []}]

        def about_shared(severity: Severity) -> list[str]:
            # Only findings about `shared`: the temporary directory's own modes vary.
            return [
                m for _, _, m in self.found(severity)
                if f"directory {shared}," in m or f"directory {shared}." in m
            ]

        refused = about_shared(Severity.REFUSE)
        self.assertEqual(len(refused), 1)
        self.assertIn("Anyone can write to `aws`'s directory", refused[0])
        os.chmod(shared, 0o775)  # nosec: B103  # Testing a group-writable directory.
        self.assertIn("Other members of its group", about_shared(Severity.WARN)[0])
        # A sticky directory (like /tmp) can't have its files replaced by others.
        os.chmod(shared, 0o1777)  # nosec: B103  # Testing a sticky directory.
        self.assertEqual(about_shared(Severity.REFUSE), [])

    def test_symlinks_mode_ignored(self) -> None:
        """A symlink's own mode (always 0777) isn't a finding; its target's is checked."""
        real_dir = self.tmp / "real"
        self.tool("aws", real_dir)
        os.symlink(real_dir, self.tmp / "linked")
        self.path_env = str(self.tmp / "linked")
        self.commands = [{"command": "aws", "args": []}]
        about = [m for _, _, m in self.found(Severity.REFUSE) if str(self.tmp / "linked") in m]
        self.assertEqual(about, [])
        os.chmod(real_dir, 0o777)  # nosec: B103  # Testing a directory others can write to.
        self.assertTrue(any(f"directory {real_dir}," in m for _, _, m in self.found(Severity.REFUSE)))

    def test_in_the_project(self) -> None:
        """A program in the project is refused."""
        self.tool("build", self.project / "tools")
        self.path_env = str(self.project / "tools")
        self.commands = [{"command": "build", "args": []}]
        refused = [m for _, _, m in self.found(Severity.REFUSE)]
        self.assertTrue(
            any("which is in the project (`scoter:project-dir`)" in m for m in refused), refused,
        )


class EgressTest(_Project):
    """Test the egress rules."""

    def host_check(self, name: str, action: str, hosts: list[str], **extra: jdata.JsonData) -> None:
        self.egress_hosts[name] = {"action": action, "reason": f"because {name}", "hosts": hosts, **extra}

    def test_http_and_data_out(self) -> None:
        """Plain `http`, and the `egress-hosts` checks (a host exactly, or under a wildcard)."""
        self.host_check("code-hosts", "warn", ["github.com", "*.githubusercontent.com"])
        self.egress = [
            {"host": "mirror.example.com", "protocol": "http"},
            {"host": "github.com", "protocol": "tls"},
            {"host": "raw.githubusercontent.com", "protocol": "tls"},
            {"host": "*.github.com", "protocol": "tls"},
            {"host": "registry.npmjs.org", "protocol": "tls"},
        ]
        found = [(key, message) for _, key, message in self.found(Severity.WARN)]
        self.assertEqual([k for k, _ in found], ["egress[0]", "egress[1]", "egress[2]"])
        self.assertEqual(found[1][1], "`github.com` (`github.com`): because code-hosts")
        self.assertEqual(
            found[2][1], "`raw.githubusercontent.com` (`*.githubusercontent.com`): because code-hosts",
        )
        # A wildcard rule that covers a listed host warns too.
        self.host_check("code-hosts", "warn", ["gist.github.com"])
        self.assertEqual([k for _, k, _ in self.found(Severity.WARN)], ["egress[0]", "egress[3]"])

    def test_actions_and_overrides(self) -> None:
        """The action decides the severity; each check that counts is shown; an override
        takes the place of the checks it names, for the hosts it matches."""
        self.host_check("code-hosts", "warn", ["github.com", "gitlab.com"])
        self.host_check("forbidden", "refuse", ["github.com", "pastebin.com"])
        self.egress = [
            {"host": "github.com", "protocol": "tls"},
            {"host": "gitlab.com", "protocol": "tls"},
            {"host": "pastebin.com", "protocol": "tls"},
        ]
        found = [(s, k, f.checks) for f in checks.check_project(UserConfig(self.suc()), self.path_env).findings
                 for s, k in [(str(f.severity), f.key)] if k.startswith("egress")]
        self.assertEqual(found, [
            ("refuse", "egress[0]", ("verification:forbidden",)),
            ("warn", "egress[0]", ("verification:code-hosts",)),
            ("warn", "egress[1]", ("verification:code-hosts",)),
            ("refuse", "egress[2]", ("verification:forbidden",)),
        ])
        # Ours: allowed for github.com only, and only in place of `code-hosts`.
        self.host_check("our-git", "allow", ["github.com"], overrides=["verification:code-hosts"])
        found = [(str(f.severity), f.key, f.checks)
                 for f in checks.check_project(UserConfig(self.suc()), self.path_env).findings
                 if f.key.startswith("egress")]
        self.assertEqual(found, [
            ("refuse", "egress[0]", ("verification:forbidden",)),
            ("note", "egress[0]", ("verification:our-git",)),
            ("warn", "egress[1]", ("verification:code-hosts",)),
            ("refuse", "egress[2]", ("verification:forbidden",)),
        ])
        # An override of a check that doesn't match does nothing.
        self.host_check("our-git", "allow", ["pastebin.com"], overrides=["verification:code-hosts"])
        self.assertIn(
            ("refuse", "egress[2]", ("verification:forbidden",)),
            [(str(f.severity), f.key, f.checks)
             for f in checks.check_project(UserConfig(self.suc()), self.path_env).findings],
        )


@unittest.skipUnless(POSIX, "symlinks and FIFOs")
class ProjectFilesTest(_Project):
    """Test the container directories."""

    def test_symlink_and_secrets(self) -> None:
        """A symlink is refused; a secret-looking file is as its check says."""
        self.project_files["secret-files"] = {"action": "warn", "reason": "secrets", "patterns": [".env", ".aws/"]}
        self.project_files["keys"] = {"action": "refuse", "reason": "keys", "patterns": ["*.pem"]}
        self.write("proj/.surf-scoter.d/env/id.pem", "x\n")
        os.symlink(self.tmp / "elsewhere", self.project / ".surf-scoter.d" / "env" / "creds")
        self.write("proj/.surf-scoter.d/env/.env", "TOKEN=x\n")
        (self.project / ".surf-scoter.d" / "env" / ".aws").mkdir()
        found = self.found()
        self.assertIn(("refuse", "env/creds"), [(s, k) for s, k, _ in found])
        warned = sorted(k for s, k, _ in found if s == "warn")
        self.assertEqual(warned, ["env/.aws", "env/.env"])
        self.assertIn(("refuse", "env/id.pem", "keys (`*.pem`)"), found)

    def test_hard_links_and_fifos(self) -> None:
        """Hard links and special files in a container directory are refused."""
        os.link(self.project / ".surf-scoter.d" / "env" / "Dockerfile", self.tmp / "other")
        os.mkfifo(self.project / ".surf-scoter.d" / "env" / "pipe")
        refused = sorted(k for _, k, _ in self.found(Severity.REFUSE))
        self.assertEqual(refused, ["env/Dockerfile", "env/pipe"])

    def test_outside_dirs_trusted(self) -> None:
        """A container directory outside the project files directory isn't checked here."""
        suc = self.suc()
        outside = self.tmp / "dist" / "base"
        outside.mkdir(parents=True)
        os.symlink(self.tmp, outside / "link")
        suc["dev-container"] = {"parent-builds": [{"dir": str(outside), "tag": "base"}]}
        self.assertEqual(checks.check_project_files(UserConfig(suc)), [])


class FromTest(unittest.TestCase):
    """Test the `FROM` digest check."""

    def test_digests(self) -> None:
        text = (
            "FROM --platform=linux/amd64 debian:bookworm AS build\n"
            "FROM build\n"
            "FROM scratch\n"
            "FROM local-base:latest\n"
            "FROM ubuntu@sha256:abc\n"
            "FROM ${BASE}\n"
            "# FROM commented:out\n"
            "from \\\n  alpine\n"
        )
        found = checks.from_findings("env/Dockerfile", text, ["local-base:latest"])
        self.assertEqual(
            [f.message.split("`")[1] for f in found],
            ["FROM debian:bookworm", "FROM ${BASE}", "FROM alpine"],
        )
        self.assertIn("build argument", found[1].message)
        self.assertTrue(all(f.severity == Severity.WARN for f in found))


class BadFileTest(_Project):
    """Test a universal configuration file that isn't what the generator writes."""

    def test_config_err(self) -> None:
        """A check without its fields, or a command without its name, is a `ConfigErr`."""
        self.tool("aws")
        self.commands = [{"command": "aws", "args": []}]
        self.host_checks["x:y"] = {"action": "refuse"}
        with self.assertRaises(checks.ConfigErr):
            self.report()
        self.host_checks.clear()
        self.commands = [{"args": []}]
        with self.assertRaises(checks.ConfigErr):
            self.report()



@unittest.skipUnless(POSIX, "modes")
class ReviewTest(_Project):
    """Test the checks on a review's accepted settings, and its overrides."""

    def setUp(self) -> None:
        super().setUp()
        self.sh = self.tool("sh")
        self.check("verification:shells", "refuse", commands={"sh": str(self.sh)})
        self.check("verification:loader-env", "refuse", env=["LD_PRELOAD"])
        self.accepted_commands: list[jdata.JsonData] = []
        self.overrides: list[jdata.JsonData] = []
        self.use: jdata.JsonData = "project"

    def command(self, program: str, **extra: jdata.JsonData) -> dict[str, jdata.JsonData]:
        return {
            "command": "run", "program": program, "args": [],
            "env": {"allow": [], "drop": [], "error": [], "set": {}, "default": {}},
            **extra,
        }

    def review(self) -> checks.Report:
        suc = self.suc()
        suc["review"] = {"result": "accept", "accepted": {
            "commands": self.accepted_commands,
            "egress": [],
            "dev-container": {"use": self.use, "images": {}},
            "overrides": {"commands": self.overrides, "egress": []},
        }}
        return checks.check_review(UserConfig(suc), self.path_env)

    def about(self, index: int) -> list[tuple[str, str]]:
        return [
            (str(f.severity), f.message) for f in self.review().findings
            if f.key.startswith(f"commands[{index}]") and (f.checks or "$override" in f.key
                                                          or "isn't on this computer" in f.message)
        ]

    def test_egress_override(self) -> None:
        """An `egress-hosts` refusal is a policy refusal: the review's `$override` sets it aside."""
        self.egress_hosts["forbidden"] = {"action": "refuse", "reason": "no", "hosts": ["pastebin.com"]}
        egress = [{"host": "pastebin.com", "protocol": "tls", "port": 443, "methods": []}]

        def egress_findings(overrides: list[jdata.JsonData]) -> list[tuple[str, str]]:
            suc = self.suc()
            suc["review"] = {"result": "accept", "accepted": {
                "commands": [], "egress": egress,
                "dev-container": {"use": self.use, "images": {}},
                "overrides": {"commands": [], "egress": overrides},
            }}
            return [(str(f.severity), f.message) for f in checks.check_review(UserConfig(suc), self.path_env).findings
                    if f.key.startswith("egress")]

        self.assertEqual(egress_findings([]), [("refuse", "`pastebin.com` (`pastebin.com`): no")])
        found = egress_findings([{"index": 0, "checks": ["verification:forbidden"]}])
        self.assertEqual([s for s, _ in found], ["note"])
        self.assertIn("You overrode `verification:forbidden`", found[0][1])

    def test_policy_override(self) -> None:
        """Refused without an override; a note with one naming the refusing check."""
        self.accepted_commands = [self.command(str(self.sh))]
        self.assertEqual(self.about(0), [("refuse", "because verification:shells")])
        self.overrides = [{"index": 0, "checks": ["verification:shells"]}]
        self.assertEqual(
            self.about(0),
            [("note", "You overrode `verification:shells` here: because verification:shells")],
        )

    def test_override_names_exactly(self) -> None:
        """A name that doesn't refuse the item is refused; an unnamed refusal stays."""
        env = {"allow": ["LD_PRELOAD"], "drop": [], "error": [], "set": {}, "default": {}}
        self.accepted_commands = [self.command(str(self.sh), env=env)]
        self.overrides = [{"index": 0, "checks": ["verification:shells", "verification:other"]}]
        found = self.about(0)
        self.assertIn(("refuse", "because verification:loader-env"), found)
        unknown = (
            "`verification:other` doesn't refuse this item; an override names only "
            "the setup checks that refuse it."
        )
        self.assertIn(("refuse", unknown), found)
        self.assertIn(
            ("note", "You overrode `verification:shells` here: because verification:shells"), found,
        )

    def test_safety_not_overridable(self) -> None:
        """A missing program stays refused, whatever the override says."""
        self.accepted_commands = [self.command(str(self.tmp / "gone"))]
        self.overrides = [{"index": 0, "checks": ["verification:shells"]}]
        severities = [s for s, m in self.about(0)]
        self.assertTrue(all(s == "refuse" for s in severities), self.about(0))
        self.assertTrue(any("Can't be overridden" in m for _, m in self.about(0)), self.about(0))

    def test_own_container_skips(self) -> None:
        """With one of the user's containers, the project's container files aren't checked."""
        os.mkfifo(self.project / ".surf-scoter.d" / "env" / "pipe")
        self.assertTrue(any(f.key == "env/pipe" for f in self.review().findings))
        self.use = {"name": "my-env"}
        self.assertFalse(any(f.key == "env/pipe" for f in self.review().findings))

    def test_rejected_or_none(self) -> None:
        """No review, or a rejected one: nothing to check."""
        self.assertEqual(checks.check_review(UserConfig(self.suc())).findings, [])
        suc = self.suc()
        suc["review"] = {"result": "reject", "accepted": None}
        self.assertEqual(checks.check_review(UserConfig(suc)).findings, [])
