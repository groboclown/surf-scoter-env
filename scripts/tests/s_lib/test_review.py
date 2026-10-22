#SPDX:MIT
"""Test the review module."""
import datetime
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from ...surf_scoter_lib import checks, fileutil, jdata, review
from ...surf_scoter_lib.user_config import UserConfig

POSIX = not fileutil.is_windows()
NOW = datetime.datetime(2026, 10, 20, 12, 0, tzinfo=datetime.UTC)


class CleanTest(unittest.TestCase):
    """Test the text cleaning and the YAML pieces."""

    def test_clean(self) -> None:
        """Control characters and escapes go; anything outside ASCII shows escaped."""
        self.assertEqual(review.clean("a\nb\tc\r"), "a b c ")
        self.assertEqual(review.clean("\x1b[31mred\x1b[0m"), "red")
        self.assertEqual(review.clean("\x1b]0;title\x07after"), "after")
        self.assertEqual(review.clean("bell\x07 del\x7f c1\x9b"), "bell del c1")
        # A right-to-left override, a zero-width space, a Cyrillic `\u0430`.
        self.assertEqual(
            review.clean("x\u202ey\u200bz \u0430"),
            "x\\u202ey\\u200bz \\u0430",
        )
        self.assertEqual(review.clean("emoji \U0001f600"), "emoji \\U0001f600")

    def test_scalars(self) -> None:
        """Text as JSON writes it: one line, ASCII, escaped."""
        self.assertEqual(review.scalar("x\n# reviewed: safe"), '"x\\n# reviewed: safe"')
        self.assertEqual(review.scalar("\u202e"), '"\\u202e"')
        self.assertEqual(review.scalar(443), "443")
        self.assertEqual(review.scalar(None), "null")
        self.assertEqual(review.key("command"), "command")
        self.assertEqual(review.key("$override"), "$override")
        self.assertEqual(review.key("AWS_*"), '"AWS_*"')
        self.assertEqual(review.key("a: b"), '"a: b"')
        self.assertEqual(
            review.flow(["s3", {"any": {"prefix": "s3://b/"}}]),
            '["s3", {any: {prefix: "s3://b/"}}]',
        )


@unittest.skipUnless(POSIX, "modes")
class BuildTest(unittest.TestCase):
    """Test the review file's layout."""

    def setUp(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.tmp = Path(os.path.realpath(directory))
        os.chmod(self.tmp, 0o700)
        self.project = self.tmp / "proj"
        files = self.project / ".surf-scoter.d"
        (files / "env").mkdir(parents=True)
        (files / "project.yaml").write_text("x: 1\n", encoding="utf-8")
        (files / "env" / "Dockerfile").write_text(
            "FROM debian:bookworm\n# \u202e hidden \u200b\nRUN \x1b[2Jtrue\n", encoding="utf-8",
        )
        self.bin = self.tmp / "bin"
        self.bin.mkdir(mode=0o700)
        for name in ("aws", "sh"):
            path = self.bin / name
            path.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(path, 0o700)

    def suc(self) -> jdata.DictJsonData:
        plan = {"allow": [], "drop": [], "error": [], "set": {}, "default": {}}
        return {
            "active-config-locations": {
                "project-dir": str(self.project),
                "dev-container-path": str(self.project / ".surf-scoter.d" / "env"),
            },
            "resolved": {
                "scoter": {"values": {
                    "project-files-dir": ".surf-scoter.d", "project-file-name": "project.yaml",
                }},
                "verification": {"values": {"expiry-days": 10}},
                "dev-env": {"values": {"network-gateway-name": "squid"}},
            },
            "current-project-setup": {
                "egress": [{"host": "registry.npmjs.org", "protocol": "tls"}],
                "env": {"set": {"CONF": "${ssh:config-file}", "NOTE": "x\n# reviewed: safe"}},
                "commands": [
                    {"command": "aws", "args": ["s3", {"any": {"prefix": "s3://b/"}}],
                     "env": {"allow": ["AWS_*"]}},
                    {"command": "sh", "args": ["-c", {"any": {}}]},
                    {"command": "gone", "args": []},
                ],
            },
            "current-project-resolved": {
                "env": {**plan, "set": {"CONF": "/home/u/.ssh/config", "NOTE": "x\n# reviewed: safe"}},
                "commands": [
                    {"env": {**plan, "allow": ["AWS_*"]}},
                    {"env": plan},
                    {"env": plan},
                ],
            },
            "host-command-checks": {"checks": {
                "verification:shells": {
                    "files": [], "action": "refuse",
                    "reason": "A shell runs anything. \x1b[31mDanger\x1b[0m \u202e",
                    "commands": {"sh": str(self.bin / "sh")}, "name-patterns": [], "options": [],
                    "env": [], "paths": [], "overrides": [],
                },
            }, "command-map": {}},
            "dev-container": {"parent-builds": []},
        }

    def build(self) -> str:
        config = UserConfig(self.suc())
        report = checks.check_project(config, str(self.bin))
        context = review.Context(
            report=report, now=NOW, files=review.project_snapshot(config), branch="main",
        )
        return review.build(config, context)

    def test_layout(self) -> None:
        """The data, with the findings as comments."""
        text = self.build()
        lines = text.splitlines()
        self.assertIn('review: "surf-scoter-review-1"', lines)
        self.assertIn("# result: reject", lines)
        self.assertIn(f'project-dir: "{self.project}"', lines)
        self.assertIn('expires-at: "2026-10-30T12:00:00+00:00"', lines)
        self.assertIn('  - {host: "registry.npmjs.org", protocol: "tls", port: 443}', lines)
        self.assertIn("#scoter:   branch         main", lines)
        self.assertIn("#scoter:   gateway        squid", lines)
        # The accepted command; the refused one commented out, with its override;
        # the missing one commented out, without.
        self.assertIn(f'    program: "{self.bin / "aws"}"', lines)
        self.assertIn('    args: ["s3", {any: {prefix: "s3://b/"}}]', lines)
        self.assertIn('  # - command: "sh"', lines)
        self.assertIn('    # $override: ["verification:shells"]', lines)
        self.assertIn('  # - command: "gone"', lines)
        gone = text[text.index('# - command: "gone"'):]
        self.assertNotIn("$override", gone.split("\n\n")[0])
        # `${...}` values resolved, with their source.
        self.assertIn("    #scoter: from `${ssh:config-file}`", lines)
        self.assertIn('    CONF: "/home/u/.ssh/config"', lines)
        self.assertIn('  use: "project"', lines)
        self.assertIn('    "debian:bookworm": "debian:bookworm"', lines)

    def test_setup_notes(self) -> None:
        """What the setup changes about the checks, or that it changes nothing; or left out."""
        config = UserConfig(self.suc())
        report = checks.check_project(config, str(self.bin))
        def build(notes: list[str] | None) -> list[str]:
            return review.build(config, review.Context(report=report, now=NOW, setup_notes=notes)).splitlines()
        lines = build(["`x:y` overrides `verification:shells` for `sh`, with `warn`"])
        self.assertIn("#scoter: How your setup changes the defaults' protections (as `scoter user-setup` shows):", lines)
        self.assertIn("#scoter:   `x:y` overrides `verification:shells` for `sh`, with `warn`", lines)
        self.assertIn("#scoter: Your setup changes none of the defaults' protections.", build([]))
        self.assertFalse(any("protections" in line for line in build(None)))

    def test_refused_egress_rule(self) -> None:
        """An egress rule an `egress-hosts` check refuses is commented out, with its `$override`."""
        suc = self.suc()
        suc["resolved"]["verification"]["egress-hosts"] = {  # type: ignore[index]
            "npm": {"action": "refuse", "reason": "not here", "hosts": ["registry.npmjs.org"]},
        }
        config = UserConfig(suc)
        lines = review.build(config, review.Context(
            report=checks.check_project(config, str(self.bin)), now=NOW,
        )).splitlines()
        self.assertIn("  #scoter: refuse: `registry.npmjs.org` (`registry.npmjs.org`): not here (verification:npm)", lines)
        self.assertIn(
            '  # - {host: "registry.npmjs.org", protocol: "tls", port: 443, $override: ["verification:npm"]}', lines,
        )

    def test_image_through_an_arg(self) -> None:
        """A `FROM` naming its image through an `ARG` is noted: `images` can't map it."""
        (self.project / ".surf-scoter.d" / "env" / "Dockerfile").write_text(
            "ARG BASE=debian:bookworm\nFROM ${BASE}\n", encoding="utf-8",
        )
        lines = self.build().splitlines()
        self.assertIn(
            "  #scoter: note: env/Dockerfile builds `FROM ${BASE}`, an image named through an `ARG`: "
            "`images` can't map it, as it isn't known until the build.",
            lines,
        )
        self.assertIn("  images: {}", lines)

    def test_values_not_comments(self) -> None:
        """A newline in a value stays inside its quotes, on one line."""
        text = self.build()
        self.assertIn('    NOTE: "x\\n# reviewed: safe"', text.splitlines())
        self.assertFalse(any(line.lstrip().startswith("# reviewed") for line in text.splitlines()))

    def test_nothing_hides(self) -> None:
        """Bidirectional and zero-width characters show escaped; terminal escapes go."""
        text = self.build()
        self.assertTrue(text.isascii())
        self.assertNotIn("\x1b", text)
        self.assertIn("\\u202e hidden \\u200b", text)
        self.assertIn("A shell runs anything. Danger \\u202e", text)
        self.assertIn("  #scoter:     | RUN true", text.splitlines())


class BranchTest(unittest.TestCase):
    """Test reading the current branch from a Git checkout's files."""

    def setUp(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.tmp = Path(directory)

    def test_branch(self) -> None:
        from ...surf_scoter_lib import vcs_branch
        (self.tmp / ".git").mkdir()
        (self.tmp / ".git" / "HEAD").write_text("ref: refs/heads/feature/x\n", encoding="utf-8")
        self.assertEqual(vcs_branch.current_branch(self.tmp), "feature/x")
        (self.tmp / ".git" / "HEAD").write_text("a" * 40 + "\n", encoding="utf-8")
        self.assertEqual(vcs_branch.current_branch(self.tmp), "detached at " + "a" * 12)
        (self.tmp / ".git" / "HEAD").write_text("nonsense\n", encoding="utf-8")
        self.assertIsNone(vcs_branch.current_branch(self.tmp))

    def test_worktree_file(self) -> None:
        """A `.git` file names the Git directory."""
        from ...surf_scoter_lib import vcs_branch
        real = self.tmp / "real-git"
        real.mkdir()
        (real / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        checkout = self.tmp / "checkout"
        checkout.mkdir()
        (checkout / ".git").write_text("gitdir: ../real-git\n", encoding="utf-8")
        self.assertEqual(vcs_branch.current_branch(checkout), "main")
        self.assertIsNone(vcs_branch.current_branch(self.tmp / "nothing"))


class CompareTest(unittest.TestCase):
    """Test comparing items across the three versions."""

    def kinds(self, section: str, old: list, mine: list, new: list) -> list[tuple[str, int | None]]:
        return [(c.kind, c.index) for c in review.compare(section, old, mine, new)]

    def test_fresh(self) -> None:
        """Without an earlier acceptance, every item is fresh."""
        self.assertEqual(self.kinds("egress", None, None, [{"host": "a"}]), [("fresh", 0)])  # type: ignore[arg-type]

    def test_each_kind(self) -> None:
        a, b, c = ({"host": h, "protocol": "tls", "port": 443} for h in "abc")
        b_methods = {**b, "methods": ["GET"]}
        mine_a = {**a, "$comment": "kept"}
        mine_x = {"host": "x", "protocol": "tls", "port": 443}
        # `c` was in the suggestion, but the user had dropped it, and so has the project: nothing to show.
        self.assertEqual(
            self.kinds("egress", [a, b, c], [mine_a, b, mine_x], [a, b_methods, {"host": "d", "protocol": "tls", "port": 443}]),
            [("unchanged", 0), ("changed", 1), ("added", 2), ("yours", None)],
        )
        self.assertEqual(self.kinds("egress", [a], [], [a]), [("removed-by-you", 0)])
        self.assertEqual(self.kinds("egress", [a], [a], []), [("removed", None)])

    def test_commands_by_name(self) -> None:
        """A command on each side once matches by name; with several forms, only identical ones match."""
        ls = {"command": "ls", "program": "/bin/ls", "args": []}
        ls_l = {**ls, "args": ["-l"]}
        mine_ls = {**ls, "args": ["-a"]}
        self.assertEqual(self.kinds("commands", [ls], [mine_ls], [ls]), [("edited", 0)])
        self.assertEqual(self.kinds("commands", [ls], [ls], [ls_l]), [("changed", 0)])
        # Two forms of `ls` now: the new one can't be told from a change, so it's added.
        self.assertEqual(
            self.kinds("commands", [ls], [ls], [ls, ls_l]), [("unchanged", 0), ("added", 1)],
        )

    def test_differences(self) -> None:
        self.assertEqual(
            review.differences({"args": [], "program": "/a", "$comment": "x"}, {"args": ["-l"], "program": "/a"}),
            ['  args: [] -> ["-l"]'],
        )


class SetupChangesTest(unittest.TestCase):
    """Test listing the setup values that changed."""

    def test_changes(self) -> None:
        from ...surf_scoter_lib.verified import records
        before = {"gateway": {"name": "squid"}, "checks": {"a": 1, "b": 2}, "vcs": "git-vcs"}
        now = {"gateway": {"name": "squid"}, "checks": {"a": 1, "b": 3, "c": 4}, "vcs": None}
        self.assertEqual(records.setup_changes(before, now), ["checks: b", "checks: c", "vcs"])


class MergeEnvTest(unittest.TestCase):
    """Test merging the top-level `env` by variable."""

    def test_merge(self) -> None:
        old = {"allow": ["LANG", "TZ"], "set": {"A": "1", "B": "2", "C": "/h/.ssh/config", "D": "4"}}
        mine = {"allow": ["LANG"], "set": {"A": "1", "B": "mine", "C": "/h/.ssh/config", "D": "my-d"}}
        new = {"allow": ["LANG", "TZ", "PAGER"], "set": {"A": "1", "B": "2", "C": "/other/config", "D": "40"}}
        raw_old = {"set": {"C": "${ssh:config-file}", "D": "4"}}
        raw_new = {"set": {"C": "${ssh:config-file}", "D": "40"}}
        merge = review.merge_env(old, mine, new, raw_old, raw_new)
        # Unchanged by the project: the user's (B), and their removal (TZ), kept.
        self.assertEqual(merge.plan["set"]["B"], "mine")
        self.assertNotIn("TZ", merge.plan["allow"])
        self.assertIn("  set: B = \"mine\" (yours)", merge.kept)
        self.assertIn("  allow: TZ (you removed it)", merge.kept)
        # New from the project.
        self.assertIn("PAGER", merge.plan["allow"])
        self.assertIn("  allow: PAGER (new)", merge.changed)
        # Changed by the setup's resolution, not the project: said so.
        self.assertEqual(merge.plan["set"]["C"], "/other/config")
        self.assertIn(
            '  set: C: your setup now gives "/other/config" (was "/h/.ssh/config"), from `${ssh:config-file}`',
            merge.changed,
        )
        # Changed by the project, after the user's edit: the project's, the user's kept aside.
        self.assertEqual(merge.plan["set"]["D"], "40")
        self.assertIn('  set: D: "4" -> "40"', merge.changed)
        self.assertEqual(merge.earlier, {("set", "D"): "my-d"})

    def test_removed_by_the_project(self) -> None:
        merge = review.merge_env({"set": {"A": "1"}}, {"set": {"A": "1"}}, {}, None, None)
        self.assertEqual(merge.plan, {})
        self.assertEqual(merge.changed, ['  set: A = "1" (the project removed it)'])

    def test_program_moved(self) -> None:
        """Only where the program is found changed: not the project's change."""
        old = {"command": "ls", "program": "/usr/bin/ls", "args": []}
        new = {**old, "program": "/opt/bin/ls"}
        shown = review.show(review.Change("changed", old, old, new, 0), [])
        assert shown is not None  # nosec: B101  # narrowing for the type checker
        self.assertTrue(shown.notes[0].startswith("found somewhere else since you accepted it"))
