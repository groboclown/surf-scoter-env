#SPDX:MIT
"""Test the main_verify script: the review prompt, and storing an acceptance."""
import contextlib
import dataclasses
import io
import json
import os
import shutil
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
from surf_scoter_lib.verified import exempt, lookup, records, store

from .dist_helper import HAVE_GENERATOR, NEEDS_GENERATOR, PROJECT_FILE, DistTest

POSIX = sys.platform != "win32"


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
        self.assertEqual(self.choose("v"), "view")
        self.assertEqual(self.choose("yes", "maybe", "u"), "use")
        self.assertEqual(self.choose("yes"), "deny")

        def interrupted(_: str) -> str:
            raise KeyboardInterrupt
        prompt = main_verify.Prompt(UserConfig({}), None, None, ask=interrupted)  # type: ignore[arg-type]
        with quiet():
            self.assertEqual(prompt.choose(), "deny")


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


class PageTest(_TempDir):
    """Test showing the review."""

    def test_pager_on_a_terminal(self) -> None:
        """With `PAGER` set and a terminal, the text goes to the pager, run in the directory given."""
        got = self.tmp / "got"
        pager = f"{sys.executable} -c \"import os, sys; open({str(got)!r}, 'w').write(os.getcwd() + '|' + sys.stdin.read())\""
        old = os.environ.get("PAGER")
        os.environ["PAGER"] = pager
        try:
            with contextlib.redirect_stdout(_Terminal()) as out:
                main_verify.page("the review\n", self.tmp)
            self.assertEqual(got.read_text(), f"{self.tmp}|the review\n")
            self.assertEqual(out.getvalue(), "")
            # Without a terminal, it's printed.
            with contextlib.redirect_stdout(io.StringIO()) as out:
                main_verify.page("the review\n", self.tmp)
            self.assertEqual(out.getvalue(), "the review\n")
        finally:
            if old is None:
                del os.environ["PAGER"]
            else:
                os.environ["PAGER"] = old

    def test_split_command(self) -> None:
        self.assertEqual(main_verify.split_command("less -R", windows=False), ["less", "-R"])
        self.assertEqual(
            main_verify.split_command('"C:\\Tools\\less.exe" -R', windows=True), ["C:\\Tools\\less.exe", "-R"],
        )


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


@unittest.skipUnless(HAVE_GENERATOR, NEEDS_GENERATOR)
class PromptTest(DistTest):
    """Test the prompt end to end, with the generator, and stand-ins for the user and their editor."""

    def setUp(self) -> None:
        super().setUp()
        self.suc = self.project_suc()
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

    def test_view(self) -> None:
        """The review is shown first, and again on `view`."""
        shown: list[str] = []
        taken = lookup.project_snapshot(self.config, self.project)
        prompt = main_verify.Prompt(
            self.config, taken, None, ask=answers("v", "d"), page=lambda text, _: shown.append(text),
        )
        text = main_verify.review.build(self.config, main_verify.review.Context(
            report=main_verify.checks.check_project(self.config), now=prompt.now(), files=taken,
        ))
        with quiet():
            self.assertEqual(prompt.run(text), 1)
        self.assertEqual(shown, [text, text])

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

    def test_egress_override(self) -> None:
        """An egress rule the setup refuses can be accepted with its `$override`, which is recorded."""
        setup = self.home / "setup.yaml"
        setup.write_text(
            setup.read_text(encoding="utf-8")
            + "  egress-hosts:\n    npm-here:\n      action: refuse\n      reason: not here\n"
            "      hosts: [registry.npmjs.org]\n",
            encoding="utf-8",
        )
        # The later runs start from the first one's setup: make it again.
        self.generate("--distribution-dir", str(self.dist), output=self.tmp / "first.json")
        self.config = UserConfig(self.project_suc())

        def keep_override(path: Path, _: Path) -> None:
            text = path.read_text(encoding="ascii")
            refused = '  # - {host: "registry.npmjs.org", protocol: "tls", port: 443, $override: ["verification:npm-here"]}'
            assert refused in text, text  # nosec: B101  # the review offers it, commented out
            path.write_text(text.replace(refused, refused.replace("# ", "", 1)), encoding="ascii")

        self.assertEqual(self.prompt("e", "u", edit=keep_override)[0], 0)
        found = lookup.find(self.config)
        assert found.accepted_settings is not None  # nosec: B101  # narrowing for the type checker
        accepted = json.loads(found.accepted_settings.read_text(encoding="utf-8"))
        self.assertEqual([r["host"] for r in accepted["egress"]], ["registry.npmjs.org"])
        self.assertEqual(accepted["overrides"]["egress"], [{"index": 0, "checks": ["verification:npm-here"]}])

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


@unittest.skipUnless(HAVE_GENERATOR, NEEDS_GENERATOR)
class WithExemptionTest(DistTest):
    """Test `scoter verify --with-exemption`: the suggestion accepted as it stands, under an exemption."""

    def setUp(self) -> None:
        super().setUp()
        # Without the shell, which the checks refuse.
        self.write_project(PROJECT_FILE.replace('  - {command: sh, args: ["-c", {any: {}}]}\n', ""))

    def write_project(self, text: str) -> None:
        (self.files / "project.yaml").write_text(text, encoding="utf-8")

    def record(self) -> records.EnvironmentRecord:
        found = records.load_environment(
            store.VerifiedStore.open(self.verified), str(self.project), store.environment_id(self.project),
        )
        assert found is not None  # nosec: B101  # narrowing for the type checker
        return found

    def accept_with_exemption(self) -> int:
        with quiet():
            return main_verify.accept_with_exemption(UserConfig(self.project_suc()))

    def start(self) -> records.Exemption:
        """Accept the project through the prompt, then start an exemption."""
        config = UserConfig(self.project_suc())
        taken = lookup.project_snapshot(config, self.project)
        prompt = main_verify.Prompt(config, taken, None, ask=answers("u"))
        text = main_verify.review.build(config, main_verify.review.Context(
            report=main_verify.checks.check_project(config), now=prompt.now(), files=taken,
        ))
        with quiet():
            self.assertEqual(prompt.run(text), 0)
        return exempt.start(config, records.now_utc())

    def test_without_an_exemption(self) -> None:
        """No exemption: nothing's accepted."""
        self.assertEqual(self.accept_with_exemption(), 1)
        self.assertFalse(self.verified.exists())

    def test_accepted_and_carried(self) -> None:
        """The changed suggestion is accepted, as `exempted`, and the exemption moves to it."""
        exemption = self.start()
        self.write_project(PROJECT_FILE.replace("registry.npmjs.org", "pypi.org").replace(
            '  - {command: sh, args: ["-c", {any: {}}]}\n', "",
        ))
        self.assertEqual(self.accept_with_exemption(), 0)
        record = self.record()
        # In place of the reviewed one: one per branch.
        [newest] = record.acceptances
        self.assertEqual(newest.kind, records.Kind.EXEMPTED)
        self.assertEqual(record.exemption, dataclasses.replace(exemption, acceptance=newest.id))
        found = lookup.find(UserConfig(self.project_suc()))
        self.assertEqual((found.match, found.acceptance), (records.MatchKind.EXACT, newest.id))
        assert found.accepted_settings is not None  # nosec: B101  # narrowing for the type checker
        accepted = json.loads(found.accepted_settings.read_text(encoding="utf-8"))
        self.assertEqual(accepted["egress"][0]["host"], "pypi.org")

    def test_refused(self) -> None:
        """What the checks refuse needs a review: nothing's accepted, and the exemption stays."""
        exemption = self.start()
        self.write_project(PROJECT_FILE)
        self.assertEqual(self.accept_with_exemption(), 1)
        record = self.record()
        self.assertEqual([a.kind for a in record.acceptances], [records.Kind.REVIEWED])
        self.assertEqual(record.exemption, exemption)
