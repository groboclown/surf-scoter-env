#SPDX:MIT
"""Test the host_file module, and the checks on a universal file's host file."""
import json
import os
import shutil
import unittest

from ...surf_scoter_lib import checks, err, host_file
from ...surf_scoter_lib.user_config import UserConfig
from ..dist_helper import HAVE_GENERATOR, NEEDS_GENERATOR, DistTest
from .test_checks import POSIX, Severity, _Project


def _entry(path: str, origin: str = "resolved") -> dict:
    return {"path": path, "real": os.path.realpath(path), "origin": origin, "sha256": None}


@unittest.skipUnless(POSIX, "file modes")
class ResolveTest(_Project):
    """Making a host file from the configuration's commands."""

    def setUp(self) -> None:
        super().setUp()
        self.commands = [
            {"command": "aws", "program": "aws-cli", "args": ["s3"]},
            {"command": "aws", "program": "aws-cli", "args": ["sts"]},
            {"command": "ls", "args": []},
        ]

    def resolve(self, previous: dict | None = None) -> host_file.Resolution:
        return host_file.resolve(UserConfig(self.suc()), self.path_env, previous)

    def test_resolve(self) -> None:
        """One entry for each program; one that isn't found has none, and is refused."""
        aws = self.tool("aws-cli")
        found = self.resolve()
        self.assertEqual(found.contents["programs"], {"aws-cli": _entry(str(aws))})
        refused = [f.key for f in found.report.refused]
        self.assertEqual(refused, ["commands[2].program"])
        self.assertEqual(found.changes, [f"`aws-cli`: added, runs {aws}"])
        host_file.check(found.contents, "made")

    def test_path_checks_every_time(self) -> None:
        """A program the `PATH` finds in the project is refused (VER-31)."""
        self.tool("aws-cli")
        self.tool("ls", self.project / "node_modules" / ".bin")
        self.path_env = os.pathsep.join([str(self.project / "node_modules" / ".bin"), str(self.bin)])
        found = self.resolve()
        messages = [f.message for f in found.report.refused]
        self.assertEqual(len(messages), 1, messages)
        self.assertIn("is in the project (`scoter:project-dir`)", messages[0])

    def test_user_entries_kept(self) -> None:
        """The developer's own paths stay, checked; resolved ones are found again."""
        self.tool("aws-cli")
        ls = self.tool("ls")
        mine = self.tool("aws-cli", self.tmp / "mine")
        previous = {"$schema": host_file.SCHEMA, "programs": {
            "aws-cli": _entry(str(mine), "user"),
            "ls": _entry("/old/ls"),
            "gone": _entry("/old/gone"),
        }}
        found = self.resolve(previous)
        self.assertEqual(found.contents["programs"], {
            "aws-cli": _entry(str(mine), "user"),
            "ls": _entry(str(ls)),
        })
        self.assertEqual(found.report.resolved[0].source, "host-file:user")
        self.assertEqual(found.changes, [
            "`gone`: removed (it ran /old/gone)",
            f"`ls`: runs {ls}, not /old/ls",
        ])
        # One that's missing is kept as written, and refused.
        os.unlink(mine)
        found = self.resolve(previous)
        self.assertEqual(found.contents["programs"]["aws-cli"]["path"], str(mine))
        self.assertIn("isn't there", found.report.refused[0].message)


class FileTest(_Project):
    """Reading and writing the file."""

    def test_round_trip(self) -> None:
        contents = {"$schema": host_file.SCHEMA, "programs": {"aws": _entry("/bin/aws", "user")}}
        path = host_file.write(self.tmp / "config", contents)
        self.assertEqual(path, self.tmp / "config" / "host.json")
        self.assertEqual(host_file.read(path), contents)
        if POSIX:
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(host_file.check(host_file.empty(), "x"), host_file.empty())

    def test_refused(self) -> None:
        good = {"$schema": host_file.SCHEMA, "programs": {"aws": _entry("/bin/aws")}}
        cases = {
            "must be a JSON object": [],
            "this isn't a host file": {**good, "$schema": "other"},
            "extra: unknown key": {**good, "extra": 1},
            "programs: must be a mapping": {**good, "programs": []},
            "programs.aws.path: must be an absolute path": {
                **good, "programs": {"aws": {**_entry("/bin/aws"), "path": "aws"}}},
            "programs.aws.origin": {**good, "programs": {"aws": {**_entry("/bin/aws"), "origin": "project"}}},
            "programs.aws.sha256": {**good, "programs": {"aws": {**_entry("/bin/aws"), "sha256": "x"}}},
            "programs.aws.more: unknown key": {**good, "programs": {"aws": {**_entry("/bin/aws"), "more": 1}}},
        }
        for message, contents in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(err.ConfigErr, message):
                host_file.check(contents, "host.json")

    @unittest.skipUnless(POSIX, "symlinks")
    def test_symlink_refused(self) -> None:
        path = host_file.write(self.tmp / "config", host_file.empty())
        os.symlink(path, self.tmp / "link.json")
        with self.assertRaisesRegex(err.ConfigErr, "may be a symlink"):
            host_file.read(self.tmp / "link.json")


@unittest.skipUnless(POSIX, "file modes")
class CheckWithHostFileTest(_Project):
    """The checks on the programs a universal file's `host-file` names."""

    def test_checks(self) -> None:
        aws = self.tool("aws-cli", self.tmp / "elsewhere")
        self.commands = [{"command": "aws", "program": "aws-cli", "args": []}, {"command": "ls", "args": []}]
        self.tool("ls")
        suc = self.suc()
        suc["host-file"] = {"file": "/c/host.json", "contents": {
            "$schema": host_file.SCHEMA, "programs": {"aws-cli": _entry(str(aws))},
        }}
        config = UserConfig(suc)
        self.assertEqual(config.host_file_programs(), {"aws-cli": _entry(str(aws))})
        report = checks.check_with_host_file(config, self.path_env)
        # Nothing is looked up: `ls` is on the `PATH`, but not in the host file.
        self.assertEqual(report.resolved[0].executable, str(aws))
        self.assertEqual(report.resolved[1].executable, None)
        self.assertEqual(
            [(f.key, f.severity) for f in report.findings if f.severity == Severity.REFUSE],
            [("commands[1].program", Severity.REFUSE)],
        )
        self.assertIn("your host file doesn't name it", report.refused[0].message)
        with self.assertRaisesRegex(err.ConfigErr, "no `host-file`"):
            checks.check_with_host_file(UserConfig(self.suc()), self.path_env)


@unittest.skipUnless(HAVE_GENERATOR, NEEDS_GENERATOR)
class GeneratorTest(DistTest):
    """The generator takes the host file this module makes."""

    def test_generator_takes_it(self) -> None:
        bin_dir = self.tmp / "tools"
        bin_dir.mkdir()
        for name in ("ls", "sh"):
            tool = bin_dir / name
            tool.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(tool, 0o700)
        suc = self.project_suc()
        found = host_file.resolve(UserConfig(suc), str(bin_dir))
        self.assertEqual(set(found.contents["programs"]), {"ls", "sh"})
        copy = self.tmp / "copy"
        shutil.copytree(self.files, copy)
        written = host_file.write(self.tmp / "config", found.contents)
        suc = self.generate(
            "--universal", str(self.tmp / "first.json"), "--no-setup-search",
            "--project-dir", str(self.project), "--config-dir", str(copy), "--host-file", str(written),
        )
        # `ls` from the `PATH`; `sh` as the setup's `command-map` maps it.
        programs = found.contents["programs"]
        self.assertEqual(programs["ls"]["path"], str(bin_dir / "ls"))
        expected = [programs["ls"]["path"], programs["sh"]["path"]]
        self.assertEqual([c["program"] for c in suc["host-brokered"]], expected)
        self.assertEqual(suc["host-file"]["contents"], json.loads(written.read_text(encoding="utf-8")))
        report = checks.check_with_host_file(UserConfig(suc), str(bin_dir))
        self.assertEqual([r.executable for r in report.resolved], expected)
