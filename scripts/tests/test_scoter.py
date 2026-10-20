#SPDX:MIT
"""Test the scoter front-end script."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

import scoter


def _config(sub_command: object) -> scoter.DictJsonData:
    """A universal configuration with the python sub-command run as given."""
    return {"resolved": {"python": {"runs": {"sub-command": sub_command}}}}  # type: ignore[dict-item]


class SubCommandPrefixTest(unittest.TestCase):
    """Test how the sub-command scripts are started."""

    script = Path("/dist/bin/main_run.py")
    python = os.path.abspath("/usr/bin/python3")

    def test_from_the_setup(self) -> None:
        """The setup's interpreter and arguments are used, with the script in place."""
        self.assertEqual(
            scoter.sub_command_prefix(
                _config([self.python, "-E", "-s", "{x:script}"]), self.script
            ),
            [self.python, "-E", "-s", str(self.script)],
        )

    def test_interpreter_not_found(self) -> None:
        """A missing interpreter leaves the command without a value."""
        with self.assertRaisesRegex(scoter.ConfigErr, "No Python interpreter"):
            scoter.sub_command_prefix(_config(None), self.script)
        with self.assertRaisesRegex(scoter.ConfigErr, "No Python interpreter"):
            scoter.sub_command_prefix({"resolved": {}}, self.script)

    def test_bad_commands(self) -> None:
        """The command must be text, name the script, and start with an absolute interpreter."""
        for bad, message in [
            ("python3 {x:script}", "to be a command"),
            ([], "to be a command"),
            ([self.python, 5, "{x:script}"], "to be a command"),
            ([self.python, "-E"], "must include `{x:script}`"),
            (["python3", "{x:script}"], "absolute path"),
        ]:
            with self.subTest(bad=bad), self.assertRaisesRegex(scoter.ConfigErr, message):
                scoter.sub_command_prefix(_config(bad), self.script)


SCRIPTS = Path(__file__).resolve().parent.parent


def _find_root(script: str, marker: str) -> list[str]:
    """A `runs: find-root` command using one of the distribution's scripts."""
    return [sys.executable, "-E", "-s", str(SCRIPTS / script), marker, "{x:start}"]


def _vcs_settings(**sections: list[str] | None) -> scoter.DictJsonData:
    """Settings with VCS sections named NAME-vcs, each with its `find-root`."""
    resolved: scoter.DictJsonData = {
        f"{name}-vcs": {"runs": {"find-root": command}}  # type: ignore[dict-item]
        for name, command in sections.items()
    }
    resolved["file-vcs"] = {"runs": {"local-clone": ["/bin/cp"]}}
    resolved["scoter"] = {
        "values": {"project-files-dir": ".surf-scoter.d", "project-file-name": "project.yaml"}
    }
    return {"resolved": resolved}


class FindVcsRootTest(unittest.TestCase):
    """Test finding the repository root with the setup's `find-root` commands."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(os.path.realpath(self._tmp.name))
        self.settings = _vcs_settings(
            git=_find_root("vcs_root_nearest.py", ".git"),
            hg=_find_root("vcs_root_nearest.py", ".hg"),
            cvs=_find_root("vcs_root_outermost.py", "CVS"),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def mkdirs(self, *paths: str) -> None:
        """Make directories under the temporary directory."""
        for path in paths:
            (self.dir / path).mkdir(parents=True, exist_ok=True)

    def find(self, start: str) -> tuple[Path, str] | None:
        """Find the root from `start`."""
        return scoter.find_vcs_root(self.settings, self.dir / start)

    def test_nearest_of_all_vcs(self) -> None:
        """Every VCS's `find-root` is run; the nearest root wins."""
        self.mkdirs("repo/.git", "repo/client/src", "repo/sub/lib")
        (self.dir / "repo/sub/.git").write_text("gitdir: ../.git/modules/sub\n")
        self.assertEqual(self.find("repo/client/src"), (self.dir / "repo", "git"))
        # Inside the submodule, it's the submodule's context; above it, the owner's.
        self.assertEqual(self.find("repo/sub/lib"), (self.dir / "repo/sub", "git"))
        # A nested repository of another VCS.
        self.mkdirs("top/CVS", "top/mod/CVS", "top/mod/inner/.hg", "top/mod/inner/x")
        self.assertEqual(self.find("top/mod"), (self.dir / "top", "cvs"))
        self.assertEqual(self.find("top/mod/inner/x"), (self.dir / "top/mod/inner", "hg"))

    def test_none(self) -> None:
        """Outside any repository, or with no `find-root`, there's no root."""
        self.mkdirs("plain/a", "repo/.git")
        self.assertIsNone(self.find("plain/a"))
        self.assertIsNone(scoter.find_vcs_root(_vcs_settings(), self.dir / "repo"))

    def test_two_vcs_with_one_root(self) -> None:
        """Two VCS giving the same root is an error."""
        self.mkdirs("both/.git", "both/.hg")
        with self.assertRaisesRegex(scoter.ConfigErr, "more than one VCS: git, hg"):
            self.find("both")

    def test_bad_commands(self) -> None:
        """A failing or unusable `find-root`, or a root that doesn't hold the start."""
        self.mkdirs("a")
        for command, message in [
            (_find_root("vcs_root_nearest.py", "../x"), "`find-root` failed"),
            ([sys.executable, "-c", "print('/elsewhere')", "{x:start}"], "doesn't hold"),
            ([sys.executable, "-c", "print('relative')", "{x:start}"], "doesn't hold"),
            ([sys.executable, "-c", "pass"], "must include `{x:start}`"),
            (["python3", "{x:start}"], "absolute path"),
            (None, "has no value"),
        ]:
            with (
                self.subTest(command=command),
                self.assertRaisesRegex(scoter.ConfigErr, message),
            ):
                scoter.find_vcs_root(_vcs_settings(git=command), self.dir / "a")


class FindProjectDirTest(unittest.TestCase):
    """Test the project search, from the start up to the VCS root."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(os.path.realpath(self._tmp.name))
        self.settings = _vcs_settings()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def project(self, at: str) -> Path:
        """Write a project file in `at`'s files directory, returning `at`."""
        path = self.dir / at / ".surf-scoter.d/project.yaml"
        path.parent.mkdir(parents=True)
        path.write_text("container: {}\n")
        return self.dir / at

    def find(self, start: str, root: str | None) -> Path | None:
        """Find the project directory from `start`, up to `root`."""
        (self.dir / start).mkdir(parents=True, exist_ok=True)
        return scoter.find_project_dir(
            self.settings, self.dir / start, self.dir / root if root else None
        )

    def test_walks_up_to_the_root(self) -> None:
        """Found in the start, or a directory above it, up to the root."""
        found = self.project("repo/client")
        self.assertEqual(self.find("repo/client/src/deep", "repo"), found)
        self.assertEqual(self.find("repo/client", None), found)
        # Without a root, only the start.
        self.assertIsNone(self.find("repo/client/src/deep", None))

    def test_not_above_the_root(self) -> None:
        """A project planted above the repository isn't found."""
        self.project("planted")
        self.assertIsNone(self.find("planted/repo/src", "planted/repo"))

    def test_nearest_project_wins(self) -> None:
        """The project nearest the start wins."""
        self.project("repo")
        inner = self.project("repo/server")
        self.assertEqual(self.find("repo/server/src", "repo"), inner)

    def test_names_from_the_setup(self) -> None:
        """The files directory and the file are the setup's."""
        values = self.settings["resolved"]["scoter"]["values"]  # type: ignore[index]
        values["project-file-name"] = "other.yaml"  # type: ignore[index]
        self.project("repo")
        self.assertIsNone(self.find("repo", None))
        (self.dir / "repo/.surf-scoter.d/other.yaml").write_text("container: {}\n")
        self.assertEqual(self.find("repo", None), self.dir / "repo")


class CheckProjectVcsTest(unittest.TestCase):
    """Test the project's `vcs` against the repository it's in."""

    def settings(self, vcs: str | None) -> scoter.DictJsonData:
        """Settings with git and hg sections, and a project using `vcs`."""
        ret = _vcs_settings(
            git=_find_root("vcs_root_nearest.py", ".git"),
            hg=_find_root("vcs_root_nearest.py", ".hg"),
        )
        ret["current-project-setup"] = {} if vcs is None else {"vcs": vcs}
        return ret

    def test_matching(self) -> None:
        """The VCS that found the root, a VCS without `find-root`, or none, is fine."""
        root = (Path("/repo"), "git")
        scoter.check_project_vcs(self.settings("git"), root)
        scoter.check_project_vcs(self.settings("file"), root)
        scoter.check_project_vcs(self.settings("file"), None)
        scoter.check_project_vcs(self.settings(None), root)

    def test_mismatch(self) -> None:
        """Another VCS's repository, or none, is an error."""
        with self.assertRaisesRegex(scoter.ConfigErr, "it's in a hg repository"):
            scoter.check_project_vcs(self.settings("git"), (Path("/repo"), "hg"))
        with self.assertRaisesRegex(scoter.ConfigErr, "isn't in a git repository"):
            scoter.check_project_vcs(self.settings("git"), None)


class WarnUnreviewedCommandsTest(unittest.TestCase):
    """Test the warning about the project's unreviewed host commands."""

    def warning(self, settings: scoter.DictJsonData) -> str:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            scoter.warn_unreviewed_commands(settings)
        return err.getvalue()

    def test_warning(self) -> None:
        """Only when the host runs commands, naming each once."""
        self.assertEqual(self.warning({"host-brokered": []}), "")
        self.assertEqual(self.warning({}), "")
        text = self.warning({"host-brokered": [{"command": "git"}, {"command": "aws"}, {"command": "git"}]})
        self.assertIn("(aws, git)", text)
        self.assertIn("can't review them yet", text)


class TempFileTest(unittest.TestCase):
    """Test the private temporary configuration files."""

    @unittest.skipIf(sys.platform == "win32", "modes")
    def test_private(self) -> None:
        """In a directory of its own, only the user's; removed with it."""
        file = scoter.save_to_tmp({"a": 1})
        directory = os.path.dirname(file)
        self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(file).st_mode & 0o777, 0o600)
        with open(file, encoding="utf-8") as inp:
            self.assertEqual(inp.read(), '{"a": 1}')
        scoter.remove_tmp(file)
        self.assertFalse(os.path.exists(directory))


class FindAcceptanceTest(unittest.TestCase):
    """Test asking the setup's `find-acceptance` for the acceptance."""

    def settings(self, script: str) -> scoter.DictJsonData:
        return {"resolved": {"verified": {"runs": {"find-acceptance": [
            sys.executable, "-c", script, "{x:universal}",
        ]}}}}

    def test_found(self) -> None:
        """Given the configuration's file, which is gone afterwards; its JSON."""
        script = (
            "import json, os, sys; "
            "print(json.dumps({'match': 'exact', 'accepted-settings': '/v/a.json', "
            "'given': os.path.isfile(sys.argv[1]), 'file': sys.argv[1]}))"
        )
        found = scoter.find_acceptance(self.settings(script))
        assert found is not None  # nosec: B101  # narrowing for the type checker
        self.assertEqual(found["accepted-settings"], "/v/a.json")
        self.assertTrue(found["given"])
        self.assertFalse(os.path.exists(str(found["file"])))

    def test_problems(self) -> None:
        """No command is no lookup; a failing or confused one is an error."""
        self.assertIsNone(scoter.find_acceptance({}))
        with self.assertRaises(scoter.ConfigErr):
            scoter.find_acceptance(self.settings("import sys; sys.exit(3)"))
        with self.assertRaises(scoter.ConfigErr):
            scoter.find_acceptance(self.settings("print('not json')"))
        with self.assertRaises(scoter.ConfigErr):
            scoter.find_acceptance(self.settings("print('[1]')"))


class RunToEndTest(unittest.TestCase):
    """Test running a sub-command."""

    def test_exit_code(self) -> None:
        """The command's exit code."""
        self.assertEqual(scoter.run_to_end([sys.executable, "-c", "import sys; sys.exit(4)"]), 4)

