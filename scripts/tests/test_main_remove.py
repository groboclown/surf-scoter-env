#SPDX:MIT
"""Test the main_remove and main_list scripts, and the remove library they use."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# The scripts import the library as their own directory's, as they do when
# they run.
SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
import main_list
import main_remove
from surf_scoter_lib import fileutil, jdata
from surf_scoter_lib.user_config import UserConfig
from surf_scoter_lib.verified import configs, remove, snapshot, store

POSIX = not fileutil.is_windows()


class StoreTest(unittest.TestCase):
    """Projects, a verified store, and configurations of the projects stored in it."""

    def setUp(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.tmp = Path(os.path.realpath(directory))
        os.chmod(self.tmp, 0o700)
        self.project = self.new_project("proj")
        self.verified = self.tmp / "verified"
        self.state = self.tmp / "state"

    def new_project(self, name: str) -> Path:
        project = self.tmp / name
        (project / ".surf-scoter.d").mkdir(parents=True)
        return project

    def config(self, project: Path | None = None, *, outside: bool = False) -> UserConfig:
        """The universal file for `project` (by default, `self.project`); `outside` any project."""
        project = project or self.project
        suc: jdata.DictJsonData = {
            "active-config-locations": {},
            "found-project": {
                "project-dir": str(project), "vcs-root": None, "vcs": None, "project-branch": None,
                "found-by": "start" if outside else "files",
            },
            "resolved": {
                "scoter": {
                    "paths": {
                        "state-dir": str(self.state),
                        "running-envs": str(self.state / "running-env.json"),
                        "running-envs-lock": str(self.state / "running-env.json.lock"),
                    },
                    "values": {"project-files-dir": ".surf-scoter.d", "project-file-name": "project.yaml"},
                },
                "verification": {"paths": {"store": str(self.verified)}},
            },
            "setup": {"files": []},
        }
        return UserConfig(suc)

    def store(self) -> store.VerifiedStore:
        return store.VerifiedStore.open(self.verified, if_missing=store.IfMissing.CREATE)

    def configure(self, text: str, name: str = "default", project: Path | None = None) -> configs.LockedConfig:
        """Store a configuration of `project` holding a project file `text`, and put it in use."""
        project = Path(os.path.realpath(project or self.project))
        env_id = store.environment_id(project)
        verified = self.store()
        taken = snapshot.snapshot_bytes([("project.yaml", text.encode(), False)], snapshot.SnapshotLimits())
        now = store.now_utc()
        made = configs.LockedConfig(configs.new_config_id(now), name, env_id, now, taken.manifest, {})
        with verified.locked():
            taken.store(verified)
            link = configs.load_link(verified, str(project), env_id) or configs.Link(env_id, str(project))
            before = link.in_use
            configs.save_config(verified, link, made, replacing=before)
            link.in_use = made.id
            configs.save_link(verified, link)
            if before is not None:
                configs.retire(verified, link, before, ())
                configs.save_link(verified, link)
        return made

    def remaining(self, project: Path | None = None) -> list[str]:
        project = Path(os.path.realpath(project or self.project))
        return sorted(c.id for c in configs.list_configs(self.store(), store.environment_id(project)))


@unittest.skipUnless(POSIX, "owner and mode checks")
class RemoveTest(StoreTest):
    """Removing configurations by name, id, or all of them."""

    def run_remove(self, *names: str, everything: bool = False, outside: bool = False) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            pick = remove.Pick.ALL if everything else remove.Pick.NAMED
            code = main_remove.run_remove(self.config(outside=outside), names, pick=pick)
        return code, out.getvalue(), err.getvalue()

    def run_list(self, *, quiet: bool = False) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            listing = main_list.Listing.IDS if quiet else main_list.Listing.FULL
            code = main_list.run_list(self.config(outside=True), listing=listing)
        return code, out.getvalue()

    def test_nothing(self) -> None:
        """No store, or nothing in it: nothing to remove or list."""
        code, out, _ = self.run_remove()
        self.assertEqual(code, 0)
        self.assertIn("nothing to remove", out)
        self.assertEqual(self.run_list(), (0, "You have no configurations.  `scoter pull` makes one from a project's files.\n"))

    def test_list_and_remove_by_name(self) -> None:
        """The project's configurations are listed; one removed by name leaves the project without one in use."""
        first = self.configure("a: 1\n", "mine")
        code, out, _ = self.run_remove()
        self.assertEqual(code, 0)
        self.assertIn(f"{first.id}  mine", out)
        self.assertIn("(in use)", out)
        code, out, err = self.run_remove("mine")
        self.assertEqual(code, 0, err)
        self.assertIn("Removed 1", out)
        self.assertEqual(self.remaining(), [])
        # With nothing left, the project is forgotten.
        self.assertEqual(self.store().index(), {})
        self.assertEqual(self.store().blob_size(), 0)

    def test_unknown_names(self) -> None:
        self.configure("a: 1\n")
        code, _, err = self.run_remove("nope")
        self.assertEqual(code, 1)
        self.assertIn("No configuration of this project is named", err)
        self.assertEqual(len(self.remaining()), 1)

    def test_by_id_in_any_project(self) -> None:
        """An id removes in any project, even outside one."""
        other = self.new_project("other")
        theirs = self.configure("b: 1\n", project=other)
        mine = self.configure("a: 1\n")
        code, out = self.run_list(quiet=True)
        self.assertEqual(sorted(out.split()), sorted([theirs.id, mine.id]))
        code, _, err = self.run_remove(theirs.id, outside=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.remaining(other), [])
        self.assertEqual(self.remaining(), [mine.id])

    def test_all_and_running(self) -> None:
        """`--all` forgets the project; one a running environment uses waits for it."""
        old = self.configure("a: 1\n", "old")
        self.configure("a: 2\n", "new")
        # The replaced one was retired already; the one in use is "running".
        running = frozenset({self.configure("a: 3\n", "third").id})
        env = remove.environment(self.config(), self.store())
        assert env is not None  # nosec: B101  # narrowing for the type checker
        done = remove.remove(env, [], running, everything=True)
        self.assertEqual(len(done), 1)
        link = env.link()
        assert link is not None  # nosec: B101  # narrowing
        self.assertEqual((link.in_use, link.removing), (None, sorted(running)))
        self.assertNotIn(old.id, self.remaining())
        # Once it stops, nothing's left.
        remove.remove(env, list(running), ())
        self.assertEqual(self.store().index(), {})

    def test_unlocked_refused(self) -> None:
        made = self.configure("a: 1\n")
        verified = self.store()
        env = remove.environment(self.config(), verified)
        assert env is not None  # nosec: B101  # narrowing
        with verified.locked():
            link = env.link()
            assert link is not None  # nosec: B101  # narrowing
            link.unlocked = configs.Unlocked(store.now_utc(), made.id)
            link.in_use = None
            configs.save_link(verified, link)
        code, _, err = self.run_remove(everything=True)
        self.assertEqual(code, 1)
        self.assertIn("unlocked", err)
        self.assertEqual(self.remaining(), [made.id])

    def test_no_project(self) -> None:
        self.configure("a: 1\n")
        code, _, err = self.run_remove("name", outside=True)
        self.assertEqual(code, 1)
        self.assertIn("There's no project here", err)
