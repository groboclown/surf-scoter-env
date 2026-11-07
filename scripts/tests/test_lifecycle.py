#SPDX:MIT
"""Test `scoter unlock`, `lock`, `switch`, `review` and `init` (`pull.lifecycle`), with the real generator."""
import os
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
from surf_scoter_lib import generator, jdata, run_config
from surf_scoter_lib.pull import analyze, lifecycle, porcelain, reviewer, unlocked
from surf_scoter_lib.user_config import UserConfig
from surf_scoter_lib.verified import configs, store

from .dist_helper import HAVE_GENERATOR, NEEDS_GENERATOR
from .test_main_pull import PROJECT_FILE, PullBase

ONLY_LS = PROJECT_FILE.replace('  - {command: sh, args: ["-c", {any: {}}]}\n', "")


class LifecycleBase(PullBase):
    """A project, its unlocked directory, and a reviewer that decides as told."""

    def setUp(self) -> None:
        super().setUp()
        (self.files / "project.yaml").write_text(ONLY_LS, encoding="utf-8")
        self.unlocked_base = self.tmp / "unlocked"
        self.real_project = os.path.realpath(self.project)
        self.env_id = store.environment_id(Path(self.real_project))
        self.root = self.unlocked_base / self.env_id
        self.lines: list[str] = []
        self.seen_sessions: list[reviewer.Session] = []
        # The phases run as sub-commands, which find programs on this `PATH`.
        original = os.environ.get("PATH", "")
        os.environ["PATH"] = self.path_env + os.pathsep + original
        self.addCleanup(os.environ.__setitem__, "PATH", original)

    def config(self, found_by: str = "files") -> UserConfig:
        config = super().config()
        suc = config.raw()
        suc["found-project"]["found-by"] = found_by  # type: ignore[index]
        pull = suc["resolved"].setdefault("pull", {})  # type: ignore[union-attr]
        pull.setdefault("paths", {})["unlocked-dir"] = str(self.unlocked_base)  # type: ignore[union-attr]
        return config

    def decide(self, decision: str) -> object:
        def review_with(config: UserConfig, workdir_id: str, session_path: Path) -> int:
            session = reviewer.read_session(session_path)
            self.seen_sessions.append(session)
            if decision == reviewer.APPROVE:
                report = analyze.analyze(config, workdir_id, generate=generator.run, path_env=self.path_env)
                session.result, session.analysis = decision, jdata.req_str(report, "id")
            else:
                session.result = decision
            reviewer.write_session(session_path, session)
            return 0
        return review_with

    def pull(self) -> None:
        code = porcelain.pull(
            self.config(), porcelain.Options(working_tree=True), say=self.lines.append,
            review_with=self.decide(reviewer.APPROVE),  # type: ignore[arg-type]
        )
        self.assertEqual(code, 0, self.lines)

    def link(self) -> configs.Link | None:
        return configs.load_link(store.VerifiedStore.open(self.verified), self.real_project, self.env_id)

    def names(self) -> dict[str, str]:
        verified = store.VerifiedStore.open(self.verified)
        link = self.link()
        removing = set(link.removing) if link else set()
        return {c.name: c.id for c in configs.list_configs(verified, self.env_id) if c.id not in removing}

    def lock(self, decision: str = reviewer.APPROVE, keep: Path | None = None) -> int:
        return lifecycle.lock(
            self.config(), porcelain.Options(), keep=keep, say=self.lines.append,
            review_with=self.decide(decision),  # type: ignore[arg-type]
        )



@unittest.skipUnless(HAVE_GENERATOR, NEEDS_GENERATOR)
class LifecycleTest(LifecycleBase):
    """The configuration's states."""

    def test_unlock_edit_lock(self) -> None:
        """Unlock, edit, lock: a new configuration in place of the one unlocked from, the copy removed."""
        self.pull()
        before = self.link()
        assert before is not None  # nosec: B101  # narrowing for the type checker
        root = lifecycle.unlock(self.config())
        self.assertEqual(root, self.root)
        link = self.link()
        assert link is not None and link.unlocked is not None  # nosec: B101  # narrowing
        self.assertEqual((link.in_use, link.unlocked.from_id), (None, before.in_use))
        self.assertTrue((root / "host.json").is_file())
        with self.assertRaisesRegex(unlocked.UnlockedErr, "already unlocked"):
            lifecycle.unlock(self.config())
        (root / "files" / "env" / "Dockerfile").write_text("FROM scratch\nRUN edited\n", encoding="utf-8")
        # A pull while unlocked would drop the edits.
        self.assertEqual(self.lock(reviewer.DENY), 1)
        self.assertIsNotNone(self.link().unlocked)  # type: ignore[union-attr]
        self.assertEqual(self.lock(), 0, self.lines)
        link = self.link()
        assert link is not None  # nosec: B101  # narrowing
        self.assertIsNone(link.unlocked)
        self.assertNotEqual(link.in_use, before.in_use)
        # The one unlocked from is retired; the timers weren't reset by the lock.
        self.assertEqual(set(self.names()), {"default"})
        self.assertEqual(link.last_reviewed, before.last_reviewed)
        self.assertFalse(root.exists())

    def test_revert_and_keep(self) -> None:
        """--revert asks before throwing edits away, goes back, and --keep renames the copy."""
        self.pull()
        in_use = self.link().in_use  # type: ignore[union-attr]
        root = lifecycle.unlock(self.config())
        (root / "files" / "env" / "Dockerfile").write_text("FROM scratch\nRUN edited\n", encoding="utf-8")
        answers = iter([""])
        self.assertEqual(lifecycle.revert(self.config(), ask=lambda _: next(answers), say=self.lines.append), 1)
        self.assertIn("  env/Dockerfile", self.lines)
        self.assertIsNotNone(self.link().unlocked)  # type: ignore[union-attr]
        for bad in (self.project / "kept", self.state / "kept", self.unlocked_base / "kept"):
            with self.subTest(bad=bad), self.assertRaisesRegex(unlocked.UnlockedErr, "is inside"):
                lifecycle.revert(self.config(), keep=bad, yes=True)
        kept = self.tmp / "kept"
        self.assertEqual(lifecycle.revert(self.config(), keep=kept, yes=True, say=self.lines.append), 0)
        link = self.link()
        assert link is not None  # nosec: B101  # narrowing
        self.assertEqual((link.in_use, link.unlocked), (in_use, None))
        self.assertFalse(root.exists())
        self.assertEqual((kept / "files" / "env" / "Dockerfile").read_text(encoding="utf-8"), "FROM scratch\nRUN edited\n")

    def test_stale_unlocked_dir(self) -> None:
        """Unlock refuses while the unlocked directory holds files, unless --replace (VER-36)."""
        self.pull()
        self.root.mkdir(parents=True)
        (self.root / "left.txt").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(unlocked.UnlockedErr, "already holds files"):
            lifecycle.unlock(self.config())
        lifecycle.unlock(self.config(), replace=True)
        backups = [p.name for p in self.unlocked_base.iterdir() if ".backup-" in p.name]
        self.assertEqual(len(backups), 1)

    def test_unlocked_dir_location(self) -> None:
        """The unlocked directory can't be in the project or the tools' own places (VER-37)."""
        self.pull()
        for inside in (self.project / "unlocked", self.state / "unlocked", self.verified / "unlocked"):
            config = self.config()
            config.raw()["resolved"]["pull"]["paths"]["unlocked-dir"] = str(inside)  # type: ignore[index]
            with self.subTest(inside=inside), self.assertRaisesRegex(unlocked.UnlockedErr, "is inside"):
                lifecycle.unlock(config)

    def test_switch(self) -> None:
        """`switch` switches by name, after the checks a run makes."""
        self.pull()
        first = self.link().in_use  # type: ignore[union-attr]
        (self.files / "env" / "Dockerfile").write_text("FROM scratch\nRUN two\n", encoding="utf-8")
        self.pull()
        self.assertEqual(lifecycle.switch(self.config(), "default.previous", generate=generator.run, path_env=self.path_env).id, first)
        self.assertEqual(self.link().in_use, first)  # type: ignore[union-attr]
        with self.assertRaisesRegex(unlocked.UnlockedErr, "no configuration 'nope'"):
            lifecycle.switch(self.config(), "nope", generate=generator.run)
        os.unlink(self.bin / "ls")
        with self.assertRaisesRegex(run_config.RunRefused, "isn't an executable"):
            lifecycle.switch(self.config(), "default", generate=generator.run, path_env=self.path_env)

    def test_review(self) -> None:
        """`review` starts from the last review's findings, and resets the timers."""
        self.pull()
        before = self.link()
        assert before is not None  # nosec: B101  # narrowing
        self.assertTrue(before.reviewed_findings)
        code = lifecycle.review_in_use(
            self.config(), porcelain.Options(), say=self.lines.append,
            review_with=self.decide(reviewer.APPROVE),  # type: ignore[arg-type]
        )
        self.assertEqual(code, 0, self.lines)
        self.assertEqual(self.seen_sessions[-1].known_findings, before.reviewed_findings)
        after = self.link()
        assert after is not None and after.last_reviewed is not None and before.last_reviewed is not None  # nosec: B101
        self.assertNotEqual(after.in_use, before.in_use)
        self.assertGreaterEqual(after.last_reviewed, before.last_reviewed)
        self.assertEqual(set(self.names()), {"default"})

    def test_init(self) -> None:
        """`init` refuses a project with files; without, it unlocks a template, and lock stores it."""
        self.assertEqual(lifecycle.init(self.config(), say=self.lines.append), 1)
        self.assertIn("already has", self.lines[-1])
        os.rename(self.files, self.tmp / "moved")
        os.symlink(self.tmp / "nowhere", self.files)
        self.assertEqual(lifecycle.init(self.config("start"), say=self.lines.append), 1)
        os.unlink(self.files)
        self.assertEqual(lifecycle.init(self.config("start"), say=self.lines.append), 0, self.lines)
        self.assertTrue((self.root / "files" / "dev-container" / "Dockerfile").is_file())
        self.assertFalse(os.path.lexists(self.files))
        link = self.link()
        assert link is not None and link.unlocked is not None  # nosec: B101  # narrowing
        self.assertEqual((link.unlocked.template, link.unlocked.from_id), ("default", None))
        # Found by its link from now on; `--revert` leaves it unconfigured.
        self.assertEqual(lifecycle.revert(self.config("link"), yes=True, say=self.lines.append), 0)
        self.assertIsNone(self.link())
        self.assertEqual(store.VerifiedStore.open(self.verified).index(), {})
        with self.assertRaisesRegex(unlocked.UnlockedErr, "no template 'nope'"):
            lifecycle.unlock(self.config("start"), template="nope")
