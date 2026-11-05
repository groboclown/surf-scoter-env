#SPDX:MIT
"""Test the default reviewer, the `scoter pull` porcelain, and cleaning up after pulls."""
import datetime
import io
import os
import shutil
import subprocess  # nosec: B404  # Runs the reviewer script without a terminal.
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path

SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
from surf_scoter_lib import cleanup, generator, jdata
from surf_scoter_lib.active_env import ActiveEnvironmentSet
from surf_scoter_lib.pull import analyze, porcelain, reviewer, workdir
from surf_scoter_lib.user_config import UserConfig
from surf_scoter_lib.verified import configs, store

from .dist_helper import HAVE_GENERATOR, NEEDS_GENERATOR
from .test_main_pull import PROJECT_FILE, PullBase


def _text(value: str, clean: str | None = None) -> dict:
    return {"text": value, "clean": clean if clean is not None else value}


def _report(analysis_id: str, findings: list[dict], files: list[dict] | None = None) -> dict:
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("refuse", "warn", "note")}
    return {
        "$schema": analyze.REPORT_SCHEMA, "id": analysis_id, "workdir": "w",
        "root": {"summary": "a summary", "counts": counts, "changed-since-base": None, "removed-since-base": []},
        "containers": [], "dev-container": {"values": {}, "project": {}},
        "files": files or [], "host-commands": [], "egress": [], "findings": findings,
        "recommendations": [],
    }


def _finding(fid: str, severity: str = "warn", key: str = "env/Dockerfile") -> dict:
    return {"id": fid, "severity": severity, "key": _text(key), "message": _text(f"message {fid}"), "checks": []}


class FakePhases:
    """Reports in order; `changes` says what `changed` answers each time."""

    def __init__(self, reports: list[dict], changes: Iterator[bool]) -> None:
        self.reports = list(reports)
        self.changes = changes
        self.analyzed = 0

    def analyze(self, workdir_id: str) -> dict:
        self.analyzed += 1
        return self.reports.pop(0)

    def changed(self, workdir_id: str, analysis_id: str) -> dict:
        changed = next(self.changes, False)
        return {"added": [], "removed": [], "changed": ["project.yaml"] if changed else [], "host-file": False}


class CompareTest(unittest.TestCase):
    """Comparing analyses by finding id (VER-28)."""

    def test_compare(self) -> None:
        first = _report("a1", [_finding("f1"), _finding("f2")])
        second = _report("a2", [_finding("f2"), _finding("f3")])
        compared = reviewer.compare(first, second, set())
        self.assertEqual((compared.new, compared.same), ({"f3"}, {"f2"}))
        self.assertEqual([f["id"] for f in compared.resolved], ["f1"])
        # Without an earlier report, a finding an earlier review showed isn't new.
        self.assertEqual(reviewer.compare(None, second, {"f2"}).new, {"f3"})


class ReviewerTest(unittest.TestCase):
    """The explorer, driven by scripted answers."""

    def setUp(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.files = Path(directory)
        (self.files / "env").mkdir()
        (self.files / "env" / "Dockerfile").write_text("FROM scratch\nRUN echo \x1b[31mred\n", encoding="utf-8")
        self.file_entry = {
            "path": _text("env/Dockerfile"), "sha256": "0" * 64, "size": 10, "executable": False,
            "base": None, "current": None, "vcs": None, "annotations": [], "findings": ["f1"],
        }

    def review(self, phases: FakePhases, answers: list[str], session: reviewer.Session | None = None) -> tuple[reviewer.Session, str]:
        out = io.StringIO()
        replies = iter(answers)

        def ask(prompt: str) -> str:
            out.write(prompt)
            try:
                return next(replies)
            except StopIteration as err:
                raise EOFError from err

        decided = reviewer.Reviewer(phases, "w", self.files, session or reviewer.Session(), ask, out).run()
        return decided, out.getvalue()

    def test_refusal_then_edit_then_approve(self) -> None:
        """A refusal blocks approval; the edit is noticed, analyzed again, and approval binds to it."""
        phases = FakePhases(
            [_report("a1", [_finding("f1", "refuse")], [self.file_entry]),
             _report("a2", [_finding("f2")], [self.file_entry])],
            iter([False, False, True, False, False]),
        )
        decided, out = self.review(phases, ["a", "", "5", "1", "", "", "a", "approve"])
        self.assertIn("finding(s) refuse it", out)
        self.assertIn("analyzing again", out)
        self.assertIn("1 no longer found", out)
        self.assertEqual((decided.result, decided.analysis), (reviewer.APPROVE, "a2"))

    def test_enter_denies(self) -> None:
        phases = FakePhases([_report("a1", [])], iter([]))
        decided, _ = self.review(phases, ["a", ""])
        self.assertEqual((decided.result, decided.analysis), (reviewer.DENY, None))
        decided, _ = self.review(FakePhases([_report("a1", [])], iter([])), [])
        self.assertEqual(decided.result, reviewer.DENY)

    def test_unopened_nudge_and_contents(self) -> None:
        """Approval lists flagged files never opened; contents are shown with escapes visible."""
        phases = FakePhases([_report("a1", [_finding("f1")], [self.file_entry])], iter([]))
        _, out = self.review(phases, ["a", "approve"])
        self.assertIn("You haven't opened", out)
        phases = FakePhases([_report("a1", [_finding("f1")], [self.file_entry])], iter([]))
        _, out = self.review(phases, ["5", "1", "", "", "a", "approve"])
        self.assertNotIn("\x1b", out)
        self.assertIn("\\x1b[31mred", out)
        self.assertNotIn("You haven't opened", out)

    def test_only_clean_text(self) -> None:
        """Project text is shown only in its clean form (VER-26)."""
        finding = _finding("f1")
        finding["message"] = _text("evil \x1b]0;title\x07", "evil")
        phases = FakePhases([_report("a1", [finding])], iter([]))
        _, out = self.review(phases, ["1", "", "q"])
        self.assertIn("evil", out)
        self.assertNotIn("\x1b", out)

    def test_opaque(self) -> None:
        """A file that can't be shown blocks approval, unless the pull allows it."""
        (self.files / "env" / "blob").write_bytes(b"\xff\xfe")
        entry = {**self.file_entry, "path": _text("env/blob"), "findings": []}
        phases = FakePhases([_report("a1", [], [entry])], iter([]))
        decided, out = self.review(phases, ["a", "q"])
        self.assertIn("can't be approved unseen", out)
        self.assertEqual(decided.result, reviewer.DENY)
        phases = FakePhases([_report("a1", [], [entry])], iter([]))
        decided, _ = self.review(phases, ["a", "approve"], reviewer.Session(allow_opaque=["env/blob"]))
        self.assertEqual(decided.result, reviewer.APPROVE)


class SessionTest(unittest.TestCase):
    def test_round_trip_and_refusals(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = Path(directory) / "session.json"
        session = reviewer.Session(["a"], ["f1"], reviewer.APPROVE, "20261103T120000Z-0123abcd")
        reviewer.write_session(path, session)
        self.assertEqual(reviewer.read_session(path), session)
        for bad in ({"$schema": "x"}, {**session.to_json(), "result": "maybe"}, {**session.to_json(), "analysis": "../x"}):
            with self.subTest(bad=bad), self.assertRaises(workdir.PullErr):
                reviewer.Session.from_json(bad)


class ScriptTest(unittest.TestCase):
    def test_needs_a_terminal(self) -> None:
        """Without a terminal, the default reviewer refuses."""
        res = subprocess.run(  # nosec: B603  # This interpreter, and the script under test.
            [sys.executable, "-E", "-s", str(SCRIPTS / "pull_reviewer.py"), "u.json", "w", "s.json"],
            stdin=subprocess.DEVNULL, capture_output=True, check=False,
        )
        self.assertEqual(res.returncode, 2)
        self.assertIn(b"needs a terminal", res.stderr)


@unittest.skipUnless(HAVE_GENERATOR, NEEDS_GENERATOR)
class PorcelainTest(PullBase):
    """`scoter pull`, with a reviewer that decides as told."""

    def setUp(self) -> None:
        super().setUp()
        (self.files / "project.yaml").write_text(
            PROJECT_FILE.replace('  - {command: sh, args: ["-c", {any: {}}]}\n', ""), encoding="utf-8",
        )

    def pull(self, decision: str, options: porcelain.Options | None = None) -> tuple[int, str, list[str]]:
        lines: list[str] = []
        seen: list[str] = []

        def decide(config: UserConfig, workdir_id: str, session_path: Path) -> int:
            seen.append(workdir_id)
            session = reviewer.read_session(session_path)
            if decision == reviewer.APPROVE:
                report = analyze.analyze(config, workdir_id, generate=generator.run, path_env=self.path_env)
                session.result, session.analysis = decision, jdata.req_str(report, "id")
            else:
                session.result = decision
            reviewer.write_session(session_path, session)
            return 0

        # The phases run as sub-commands, which find programs on this `PATH`.
        original = os.environ.get("PATH", "")
        os.environ["PATH"] = self.path_env + os.pathsep + original
        self.addCleanup(os.environ.__setitem__, "PATH", original)
        code = porcelain.pull(self.config(), options or porcelain.Options(working_tree=True), say=lines.append, review_with=decide)
        return code, "\n".join(lines), seen

    def work(self) -> list[str]:
        directory = self.state / workdir.PULL_DIR / workdir.WORK_DIR
        return os.listdir(directory) if directory.is_dir() else []

    def test_approve_deny_and_nothing_to_pull(self) -> None:
        code, out, _ = self.pull(reviewer.DENY)
        self.assertEqual(code, 1)
        self.assertIn("Not approved", out)
        self.assertEqual(self.work(), [])
        code, out, _ = self.pull(reviewer.APPROVE)
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"the configuration `default` \(\S+\) is now in use")
        self.assertEqual(self.work(), [])
        link = configs.load_link(
            store.VerifiedStore.open(self.verified), os.path.realpath(self.project),
            store.environment_id(Path(os.path.realpath(self.project))),
        )
        self.assertIsNotNone(link and link.in_use)
        code, out, seen = self.pull(reviewer.APPROVE)
        self.assertEqual((code, seen), (0, []))
        self.assertIn("Nothing to pull", out)

    def test_keep_working_dir(self) -> None:
        code, out, _ = self.pull(reviewer.DENY, porcelain.Options(working_tree=True, keep_working_dir=True))
        self.assertEqual(code, 1)
        self.assertIn("Kept the working directory", out)
        self.assertEqual(len(self.work()), 1)
        # `scoter cleanup` takes it once it's old.
        later = datetime.datetime.now(datetime.UTC) + cleanup.PULL_AGE * 2
        found = cleanup._orphaned_state(self.state, ActiveEnvironmentSet(file=self.tmp / "none.json", environs=[]), later)
        self.assertEqual(sorted(p.parent.name for p in found), ["records", "work"])
        now = datetime.datetime.now(datetime.UTC)
        self.assertEqual(cleanup._orphaned_state(self.state, ActiveEnvironmentSet(file=self.tmp / "none.json", environs=[]), now), [])
