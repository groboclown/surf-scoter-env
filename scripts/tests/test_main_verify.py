#SPDX:MIT
"""Test the main_verify script: the review prompt, and storing an acceptance."""
import contextlib
import io
import json
import os
import shutil
import subprocess  # nosec: B404  # Runs the generator to make the test's universal file.
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path

# The scripts import the library as their own directory's, as they do when
# they run.
SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
import main_verify
from surf_scoter_lib.user_config import UserConfig
from surf_scoter_lib.verified import lookup, records, store

REPO = SCRIPTS.parent
GENERATOR = REPO / "host" / "target" / "debug" / "gen-scoter-uc"
POSIX = sys.platform != "win32"

PROJECT_FILE = """\
container:
  dev-container-path: ./env
egress:
  - {host: registry.npmjs.org, protocol: tls}
commands:
  - {command: ls, args: [{any: {}}]}
  - {command: sh, args: ["-c", {any: {}}]}
"""


def quiet() -> contextlib.ExitStack:
    """Keep the prompt's output out of the test's."""
    stack = contextlib.ExitStack()
    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
    return stack


def answers(*given: str) -> Callable[[str], str]:
    """Answers the prompt with `given`, then ends the input."""
    pending: Iterator[str] = iter(given)

    def ask(_: str) -> str:
        try:
            return next(pending)
        except StopIteration:
            raise EOFError from None
    return ask


class _TempDir(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.tmp = Path(os.path.realpath(directory))
        os.chmod(self.tmp, 0o700)


class NoTerminalTest(unittest.TestCase):
    """Without a terminal, nothing is asked, and nothing accepted."""

    def test_no_terminal(self) -> None:
        config = UserConfig({"current-project-setup": {"commands": []}})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main_verify.run_verify(config, None, interactive=False), 1)
        self.assertIn("needs a terminal", err.getvalue())


class ChooseTest(unittest.TestCase):
    """Test reading the choice."""

    def choose(self, *given: str) -> str:
        prompt = main_verify.Prompt(UserConfig({}), None, None, ask=answers(*given))  # type: ignore[arg-type]
        with quiet():
            return prompt.choose()

    def test_choices(self) -> None:
        """Enter, end of input and Ctrl-C deny; anything else asks again."""
        self.assertEqual(self.choose(""), "deny")
        self.assertEqual(self.choose(), "deny")
        self.assertEqual(self.choose("u"), "use")
        self.assertEqual(self.choose(" Edit "), "edit")
        self.assertEqual(self.choose("yes", "maybe", "u"), "use")
        self.assertEqual(self.choose("yes"), "deny")

        def interrupted(_: str) -> str:
            raise KeyboardInterrupt
        prompt = main_verify.Prompt(UserConfig({}), None, None, ask=interrupted)  # type: ignore[arg-type]
        with quiet():
            self.assertEqual(prompt.choose(), "deny")


class EditorCommandTest(unittest.TestCase):
    """Test choosing the editor."""

    def test_visual_then_editor(self) -> None:
        both = {"VISUAL": "code --wait", "EDITOR": "vi"}
        self.assertEqual(main_verify.editor_command(both, windows=False), ["code", "--wait"])
        self.assertEqual(main_verify.editor_command({"EDITOR": "vi"}, windows=False), ["vi"])
        self.assertEqual(main_verify.editor_command({"VISUAL": "", "EDITOR": "vi"}, windows=False), ["vi"])

    def test_neither(self) -> None:
        """Elsewhere, nothing is guessed; on Windows, Notepad, by its full path."""
        with self.assertRaisesRegex(main_verify.ReviewFileErr, "VISUAL or EDITOR"):
            main_verify.editor_command({}, windows=False)
        self.assertEqual(
            main_verify.editor_command({"SystemRoot": "D:\\Win"}, windows=True),
            [os.path.join("D:\\Win", "System32", "notepad.exe")],
        )
        self.assertEqual(
            main_verify.editor_command({}, windows=True),
            [os.path.join("C:\\Windows", "System32", "notepad.exe")],
        )

    def test_windows_paths(self) -> None:
        """Backslashes stay, and quotes around a path with spaces go."""
        command = '"C:\\Program Files\\Editor\\ed.exe" --wait'
        self.assertEqual(
            main_verify.editor_command({"EDITOR": command}, windows=True),
            ["C:\\Program Files\\Editor\\ed.exe", "--wait"],
        )


@unittest.skipUnless(POSIX, "links and modes")
class ReadOnceTest(_TempDir):
    """Test reading the edited file."""

    def test_regular(self) -> None:
        path = self.tmp / "review.yaml"
        path.write_bytes(b"review: x\n")
        self.assertEqual(main_verify.read_once(path), b"review: x\n")

    def test_link(self) -> None:
        """A link in its place is refused, not followed."""
        (self.tmp / "other").write_text("x", encoding="utf-8")
        os.symlink(self.tmp / "other", self.tmp / "review.yaml")
        with self.assertRaisesRegex(main_verify.ReviewFileErr, "replaced by a link"):
            main_verify.read_once(self.tmp / "review.yaml")

    def test_not_a_regular_file(self) -> None:
        """A FIFO (which would hang a plain read) and a hard-linked file are refused."""
        os.mkfifo(self.tmp / "review.yaml")
        with self.assertRaisesRegex(main_verify.ReviewFileErr, "regular file"):
            main_verify.read_once(self.tmp / "review.yaml")
        os.unlink(self.tmp / "review.yaml")
        (self.tmp / "review.yaml").write_text("x", encoding="utf-8")
        os.link(self.tmp / "review.yaml", self.tmp / "other")
        with self.assertRaisesRegex(main_verify.ReviewFileErr, "other links"):
            main_verify.read_once(self.tmp / "review.yaml")

    def test_too_large(self) -> None:
        path = self.tmp / "review.yaml"
        path.write_bytes(b"#" * (main_verify.MAX_REVIEW_SIZE + 1))
        with self.assertRaisesRegex(main_verify.ReviewFileErr, "larger"):
            main_verify.read_once(path)


@unittest.skipUnless(POSIX and GENERATOR.is_file(), "needs the generator (cd host && cargo build)")
class PromptTest(_TempDir):
    """Test the prompt end to end, with the generator, and stand-ins for the user and their editor."""

    def setUp(self) -> None:
        super().setUp()
        # A distribution like an installed one: the generator, the scripts, the defaults.
        dist = self.tmp / "dist"
        (dist / "bin").mkdir(parents=True)
        os.symlink(GENERATOR, dist / "bin" / "gen-scoter-uc")
        for script in SCRIPTS.glob("*.py"):
            os.symlink(script, dist / "bin" / script.name)
        os.symlink(SCRIPTS / "surf_scoter_lib", dist / "bin" / "surf_scoter_lib")
        os.symlink(REPO / "defaults", dist / "defaults")
        os.symlink(REPO / "containers", dist / "containers")
        shutil.copy(REPO / "defaults" / "setup.yaml", dist / "setup.yaml")
        self.verified = self.tmp / "verified"
        home = self.tmp / "home"
        home.mkdir()
        (home / "setup.yaml").write_text(
            f"scoter:\n  paths:\n    verified-dir: {json.dumps(str(self.verified))}\n", encoding="utf-8",
        )
        self.project = self.tmp / "proj"
        files = self.project / ".surf-scoter.d"
        (files / "env").mkdir(parents=True)
        (files / "project.yaml").write_text(PROJECT_FILE, encoding="utf-8")
        (files / "env" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        shutil.copy(REPO / "containers" / "dev-env" / "dev-env.yaml", files / "env" / "dev-env.yaml")

        env = {**os.environ, "SURF_SCOTER_HOME": str(home)}
        first = self.tmp / "first.json"
        for args in (
            ["--distribution-dir", str(dist), "--output", str(first)],
            ["--universal", str(first), "--no-setup-search", "--project-dir", str(self.project),
             "--output", str(self.tmp / "suc.json")],
        ):
            subprocess.run(  # nosec: B603  # The generator under test.
                [str(GENERATOR), *args], env=env, check=True, capture_output=True,
            )
        self.suc = json.loads((self.tmp / "suc.json").read_text(encoding="utf-8"))
        self.config = UserConfig(self.suc)

    def prompt(
        self, *given: str, edit: Callable[[Path, Path], None] | None = None,
    ) -> tuple[int, main_verify.Prompt]:
        """Run the prompt on the review made from the project, as `scoter verify` does."""
        taken = lookup.project_snapshot(self.config, self.project)
        prompt = main_verify.Prompt(self.config, taken, "main", ask=answers(*given), edit=edit)
        text = main_verify.review.build(self.config, main_verify.review.Context(
            report=main_verify.checks.check_project(self.config),
            now=prompt.now(), files=taken, branch="main",
        ))
        with quiet():
            return prompt.run(text), prompt

    def stored(self) -> list[records.Acceptance]:
        if not self.verified.exists():
            return []
        verified_store = store.VerifiedStore.open(self.verified)
        record = records.load_environment(
            verified_store, str(self.project), store.environment_id(self.project),
        )
        return [] if record is None else record.acceptances

    def test_use(self) -> None:
        """Used as it is: stored, and the next lookup finds it."""
        code, _ = self.prompt("u")
        self.assertEqual(code, 0)
        [acceptance] = self.stored()
        self.assertEqual((acceptance.kind, acceptance.branch), (records.Kind.REVIEWED, "main"))
        found = lookup.find(self.config)
        self.assertEqual((found.match, found.acceptance), (records.MatchKind.EXACT, acceptance.id))
        assert found.accepted_settings is not None  # nosec: B101  # narrowing for the type checker
        accepted = json.loads(found.accepted_settings.read_text(encoding="utf-8"))
        # The refused shell stays out: it was commented out in the review.
        self.assertEqual([c["command"] for c in accepted["commands"]], ["ls"])
        self.assertEqual(accepted["egress"][0]["host"], "registry.npmjs.org")
        where = found.accepted_settings.parent
        self.assertTrue((where / "review.yaml").read_text(encoding="ascii").startswith("#scoter:"))
        suggestion = json.loads((where / "suggestion.json").read_text(encoding="utf-8"))
        self.assertEqual(suggestion["project"]["commands"][1]["command"], "sh")
        for path in (where / "accepted.json", where / "review.yaml", where / "suggestion.json"):
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_deny(self) -> None:
        """Enter denies; so does the end of the input."""
        self.assertEqual(self.prompt("")[0], 1)
        self.assertEqual(self.prompt()[0], 1)
        self.assertEqual(self.stored(), [])

    def test_edit_adds_a_refused_program(self) -> None:
        """Uncommenting the refused shell, without its override: checked again, and refused."""
        seen: list[tuple[Path, int]] = []

        def add_shell(path: Path, directory: Path) -> None:
            seen.append((directory, os.stat(directory).st_mode & 0o777))
            lines = path.read_text(encoding="ascii").splitlines()
            start = lines.index('  # - command: "sh"')
            for i in range(start, len(lines)):
                if not lines[i].strip():
                    break
                if "$override" not in lines[i]:
                    lines[i] = lines[i].replace("# ", "", 1)
            path.write_text("\n".join(lines) + "\n", encoding="ascii")

        code, _ = self.prompt("e", "u", "d", edit=add_shell)
        self.assertEqual(code, 1)
        self.assertEqual(self.stored(), [])
        # The editor ran in the private temporary directory, not the project.
        [(directory, mode)] = seen
        self.assertEqual(mode, 0o700)
        self.assertFalse(directory.is_relative_to(self.project))
        self.assertFalse(directory.exists())

    def test_edit_with_its_override(self) -> None:
        """With its `$override` too, the shell's refusal is set aside, and it's stored."""
        def add_shell(path: Path, _: Path) -> None:
            text = path.read_text(encoding="ascii")
            for line in ('  # - command: "sh"', '    # program: ', '    # args: ', '    # $override: '):
                text = text.replace(line, line.replace("# ", "", 1))
            path.write_text(text, encoding="ascii")

        self.assertEqual(self.prompt("e", "u", edit=add_shell)[0], 0)
        found = lookup.find(self.config)
        assert found.accepted_settings is not None  # nosec: B101  # narrowing for the type checker
        accepted = json.loads(found.accepted_settings.read_text(encoding="utf-8"))
        self.assertEqual([c["command"] for c in accepted["commands"]], ["ls", "sh"])

    def test_replaced_by_a_link(self) -> None:
        """The file swapped for a link while the editor is open: refused, nothing stored."""
        target = self.tmp / "planted.yaml"

        def swap(path: Path, _: Path) -> None:
            shutil.copy(path, target)
            os.unlink(path)
            os.symlink(target, path)

        self.assertEqual(self.prompt("e", "u", edit=swap)[0], 1)
        self.assertEqual(self.stored(), [])

    def test_broken_edit(self) -> None:
        """A file the generator won't read can't be used; it names the file being edited."""
        edited: list[Path] = []

        def break_it(path: Path, _: Path) -> None:
            edited.append(path)
            path.write_text(path.read_text(encoding="ascii") + "unknown: 1\n", encoding="ascii")

        err = io.StringIO()
        taken = lookup.project_snapshot(self.config, self.project)
        prompt = main_verify.Prompt(self.config, taken, None, ask=answers("e", "u"), edit=break_it)
        text = main_verify.review.build(self.config, main_verify.review.Context(
            report=main_verify.checks.check_project(self.config), now=prompt.now(), files=taken,
        ))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(prompt.run(text), 1)
        self.assertIn(f"error: gen-scoter-uc: {edited[0]}: unknown: unknown key", err.getvalue())
        self.assertEqual(self.stored(), [])

    def test_reject(self) -> None:
        """`result: reject` uncommented: using it denies."""
        def reject(path: Path, _: Path) -> None:
            text = path.read_text(encoding="ascii").replace("# result: reject", "result: reject")
            path.write_text(text, encoding="ascii")

        self.assertEqual(self.prompt("e", "u", edit=reject)[0], 1)
        self.assertEqual(self.stored(), [])


if __name__ == "__main__":
    unittest.main()
