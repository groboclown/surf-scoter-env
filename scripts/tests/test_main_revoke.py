#SPDX:MIT
"""Test the main_revoke and main_list_accepted scripts, and the revoke library they use."""
import contextlib
import datetime
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
import main_list_accepted
import main_revoke
from surf_scoter_lib import fileutil, jdata
from surf_scoter_lib.user_config import UserConfig
from surf_scoter_lib.verified import accept, lookup, records, revoke, store

POSIX = not fileutil.is_windows()


class StoreTest(unittest.TestCase):
    """Projects, a verified store, and acceptances of the projects stored in it."""

    def setUp(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.tmp = Path(os.path.realpath(directory))
        os.chmod(self.tmp, 0o700)
        self.project = self.new_project("proj")
        self.verified = self.tmp / "verified"

    def new_project(self, name: str) -> Path:
        project = self.tmp / name
        files = project / ".surf-scoter.d"
        (files / "env").mkdir(parents=True)
        (files / "project.yaml").write_text("container: {dev-container-path: env}\n", encoding="utf-8")
        return project

    def config(
        self, project: Path | None = None, *, outside: bool = False, branch: str | None = None,
    ) -> UserConfig:
        """The universal file for `project` (by default, `self.project`), on `branch`; `outside` any project."""
        project = project or self.project
        locations: jdata.DictJsonData = {} if outside else {
            "project-dir": str(project),
            "dev-container-path": str(project / ".surf-scoter.d" / "env"),
            "project-branch": branch,
        }
        suc: jdata.DictJsonData = {
            "active-config-locations": locations,
            "resolved": {
                "scoter": {
                    "path-lists": {"dev-containers-search": []},
                    "values": {"project-files-dir": ".surf-scoter.d", "project-file-name": "project.yaml"},
                },
                "verification": {
                    "paths": {"store": str(self.verified)},
                    "path-lists": {"verified-containers": []},
                },
            },
            "current-project-setup": {"container": {"dev-container-path": "env"}},
            "dev-container": {"user": "me", "parent-builds": []},
            "setup": {"files": []},
        }
        return UserConfig(suc)

    def accept(
        self, dockerfile: str, branch: str | None, project: Path | None = None,
    ) -> records.Acceptance:
        """Accept the project (by default, `self.project`) with this Dockerfile, on `branch`."""
        project = project or self.project
        (project / ".surf-scoter.d" / "env" / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        config = self.config(project)
        now = records.now_utc() - datetime.timedelta(seconds=1)
        expires = (now + datetime.timedelta(days=3)).isoformat()
        return accept.store_acceptance(
            config, lookup.project_snapshot(config, project), b"review: x\n",
            {"expires-at": expires, "commands": []}, branch, now,
        )

    def remaining(self, project: Path | None = None) -> list[str]:
        verified_store = store.VerifiedStore.open(self.verified)
        env = revoke.environment(self.config(project), verified_store)
        assert env is not None  # nosec: B101  # narrowing for the type checker
        record = env.record()
        return [] if record is None else [a.id for a in record.acceptances]

    def acceptance_dir(self, acceptance: records.Acceptance) -> Path:
        env_id = store.environment_id(self.project)
        return self.verified / env_id / records.ACCEPTANCES_DIR / acceptance.id


@unittest.skipUnless(POSIX, "owner and mode checks")
class RevokeTest(StoreTest):
    """Revoking acceptances by branch, fingerprint, or all of them."""

    def run_revoke(
        self, *names: str, everything: bool = False, outside: bool = False,
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            pick = revoke.Pick.ALL if everything else revoke.Pick.NAMED
            code = main_revoke.run_revoke(self.config(outside=outside), names, pick=pick)
        return code, out.getvalue(), err.getvalue()

    def run_list(self, *, quiet: bool = False) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            listing = main_list_accepted.Listing.IDS if quiet else main_list_accepted.Listing.FULL
            code = main_list_accepted.run_list(self.config(outside=True), listing=listing)
        return code, out.getvalue()

    def test_nothing_accepted(self) -> None:
        """No store, or nothing in it: nothing to revoke."""
        code, out, _ = self.run_revoke()
        self.assertEqual(code, 0)
        self.assertIn("nothing to revoke", out)
        self.assertEqual(self.run_revoke("main")[0], 1)
        self.assertEqual(self.run_list(), (0, "You haven't accepted any project's settings.\n"))
        self.assertEqual(self.run_list(quiet=True), (0, ""))

    def test_list(self) -> None:
        """Without names or `--all`, the acceptances are listed and none revoked."""
        main = self.accept("FROM scratch\n", "main")
        code, out, _ = self.run_revoke()
        self.assertEqual(code, 0)
        self.assertIn(f"  {main.id}  reviewed  branch main  fingerprint ", out)
        self.assertEqual(self.remaining(), [main.id])

    def test_by_branch(self) -> None:
        """Each branch named, and only those; their files go too."""
        main = self.accept("FROM scratch\n", "main")
        feature = self.accept("FROM debian\n", "feature/x")
        other = self.accept("FROM alpine\n", "other")
        code, out, _ = self.run_revoke("main", "feature/x")
        self.assertEqual(code, 0)
        self.assertIn("Revoked 2:", out)
        self.assertEqual(self.remaining(), [other.id])
        self.assertFalse(self.acceptance_dir(main).exists())
        self.assertFalse(self.acceptance_dir(feature).exists())
        self.assertTrue(self.acceptance_dir(other).exists())

    def test_by_fingerprint(self) -> None:
        """A whole fingerprint, with or without `sha256:`, or its first 12 characters or more."""
        first = self.accept("FROM scratch\n", "main")
        second = self.accept("FROM debian\n", "dev")
        third = self.accept("FROM alpine\n", None)
        assert first.suggestion and second.suggestion and third.suggestion  # nosec: B101  # narrowing for the type checker
        whole = first.suggestion.id
        short = second.suggestion.id.removeprefix("sha256:")[:12].upper()
        self.assertEqual(self.run_revoke(whole, short)[0], 0)
        self.assertEqual(self.remaining(), [third.id])
        # Shorter than 12 isn't taken as a fingerprint.
        code, _, err = self.run_revoke(third.suggestion.id.removeprefix("sha256:")[:11])
        self.assertEqual(code, 1)
        self.assertIn("No acceptance has the id, or is for the branch or fingerprint", err)

    def test_carried_forward_replaces(self) -> None:
        """An acceptance carried forward takes the place of the one it came from, and its files."""
        original = self.accept("FROM scratch\n", "main")
        (self.project / ".surf-scoter.d" / "env" / "Dockerfile").write_text(
            "# A comment.\nFROM scratch\n", encoding="utf-8",
        )
        found = lookup.find(self.config())
        self.assertEqual(found.match, records.MatchKind.MEANING)
        self.assertEqual(self.remaining(), [found.acceptance])
        self.assertFalse(self.acceptance_dir(original).exists())
        self.assertEqual(self.run_revoke("main")[0], 0)
        self.assertEqual(store.VerifiedStore.open(self.verified).index(), {})

    def test_unmatched_revokes_none(self) -> None:
        """A name that chooses nothing (a typo, say): nothing is revoked, and the list is shown."""
        main = self.accept("FROM scratch\n", "main")
        code, _, err = self.run_revoke("main", "mian")
        self.assertEqual(code, 1)
        self.assertIn("`mian`", err)
        self.assertIn("Nothing was revoked", err)
        self.assertEqual(self.remaining(), [main.id])

    def test_all(self) -> None:
        """`--all`: every acceptance, and the blobs only they used."""
        self.accept("FROM scratch\n", "main")
        self.accept("FROM debian\n", None)
        code, out, _ = self.run_revoke(everything=True)
        self.assertEqual(code, 0)
        self.assertIn("Revoked 2:", out)
        self.assertEqual(self.remaining(), [])
        self.assertEqual(os.listdir(self.verified / "blobs"), [])
        # The project, left with none, is gone from the store and its index.
        self.assertEqual(store.VerifiedStore.open(self.verified).index(), {})
        self.assertFalse((self.verified / store.environment_id(self.project)).exists())
        # The next run finds nothing.
        self.assertEqual(lookup.find(self.config()), lookup.Lookup())

    def test_by_id_in_any_project(self) -> None:
        """An id revokes that acceptance, in whichever project, from outside any project too."""
        other = self.new_project("other")
        mine = self.accept("FROM scratch\n", "main")
        theirs = self.accept("FROM scratch\n", "main", other)
        kept = self.accept("FROM alpine\n", "dev", other)
        code, out, _ = self.run_revoke(mine.id, theirs.id, outside=True)
        self.assertEqual(code, 0)
        self.assertIn("Revoked 2:", out)
        self.assertIn(f"  {other}\n    {theirs.id}", out)
        self.assertEqual(self.remaining(other), [kept.id])
        # Its last acceptance gone, the project is gone from the index.
        self.assertEqual(
            store.VerifiedStore.open(self.verified).index(), {str(other): store.environment_id(other)},
        )

    def test_unknown_id_outside(self) -> None:
        """An id no acceptance has, outside a project: said so, and nothing revoked."""
        kept = self.accept("FROM scratch\n", "main")
        code, _, err = self.run_revoke(kept.id, "20000101T000000Z-00000000", outside=True)
        self.assertEqual(code, 1)
        self.assertIn("No acceptance has the id `20000101T000000Z-00000000`", err)
        self.assertEqual(self.remaining(), [kept.id])
        # Branches and fingerprints need a project.
        code, _, err = self.run_revoke("main", outside=True)
        self.assertEqual(code, 1)
        self.assertIn("There's no project here", err)

    def test_list_accepted(self) -> None:
        """Every project, each acceptance with its id, expiry and exemption; `-q`, only the ids."""
        other = self.new_project("other")
        first = self.accept("FROM scratch\n", "main")
        second = self.accept("FROM scratch\n", None, other)
        code, out = self.run_list()
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], str(other))
        self.assertTrue(lines[1].startswith(f"  {second.id}  reviewed  branch -  fingerprint "))
        self.assertIn(f"expires {second.expires_at.isoformat()} (valid)", lines[1])
        self.assertEqual(lines[2], str(self.project))
        self.assertTrue(lines[3].startswith(f"  {first.id}  reviewed  branch main  "))
        self.assertIn("2 acceptances of 2 projects", lines[4])
        self.assertEqual(self.run_list(quiet=True), (0, f"{second.id}\n{first.id}\n"))

    def test_list_exemption_and_gone(self) -> None:
        """An exemption is shown, and so is a project whose directory has gone."""
        acceptance = self.accept("FROM scratch\n", "main")
        verified_store = store.VerifiedStore.open(self.verified)
        env = revoke.environment(self.config(), verified_store)
        assert env is not None  # nosec: B101  # narrowing for the type checker
        record = env.record()
        assert record is not None  # nosec: B101  # narrowing for the type checker
        exempt = acceptance.accepted_at + datetime.timedelta(days=1)
        record.exemption = records.Exemption(acceptance.id, exempt, "main", acceptance.accepted_at)
        with verified_store.locked():
            records.save_environment(verified_store, record)
        shutil.rmtree(self.project)
        out = self.run_list()[1]
        self.assertIn(f"{self.project}  (the directory is gone)", out)
        self.assertIn(f"exempt until {exempt.isoformat()} (holds)  branch main  from {acceptance.id}", out)

    def test_revoke_ends_exemption(self) -> None:
        """Revoking the acceptance an exemption uses ends the exemption; revoking another doesn't."""
        first = self.accept("FROM scratch\n", "dev")
        second = self.accept("FROM alpine\n", "main")
        self.accept("FROM debian\n", None)
        verified_store = store.VerifiedStore.open(self.verified)

        def record() -> records.EnvironmentRecord:
            env = revoke.environment(self.config(), verified_store)
            assert env is not None  # nosec: B101  # narrowing for the type checker
            found = env.record()
            assert found is not None  # nosec: B101  # narrowing for the type checker
            return found

        exempting = record()
        until = records.now_utc() + datetime.timedelta(hours=1)
        exempting.exemption = records.Exemption(second.id, until, "main", records.now_utc())
        with verified_store.locked():
            records.save_environment(verified_store, exempting)
        self.assertEqual(self.run_revoke(first.id)[0], 0)
        self.assertIsNotNone(record().exemption)
        self.assertEqual(self.run_revoke(second.id)[0], 0)
        self.assertIsNone(record().exemption)

    def test_clean_out_everything(self) -> None:
        """`scoter revoke $(scoter list-accepted -q)`, from anywhere: nothing's left."""
        other = self.new_project("other")
        self.accept("FROM scratch\n", "main")
        self.accept("FROM alpine\n", "dev")
        self.accept("FROM scratch\n", None, other)
        ids = self.run_list(quiet=True)[1].split()
        self.assertEqual(len(ids), 3)
        code, out, _ = self.run_revoke(*ids, outside=True)
        self.assertEqual(code, 0)
        self.assertIn("Revoked 3:", out)
        self.assertEqual(self.run_list(quiet=True), (0, ""))
        verified_store = store.VerifiedStore.open(self.verified)
        self.assertEqual(verified_store.index(), {})
        self.assertEqual(os.listdir(self.verified / "blobs"), [])

    def test_all_with_names(self) -> None:
        """`--all` and names together is a usage error."""
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main_revoke.main(["main_revoke.py", self.write_suc(), "revoke", "--all", "main"])
        self.assertEqual(caught.exception.code, 2)

    def write_suc(self) -> str:
        import json
        path = self.tmp / "suc.json"
        path.write_text(json.dumps(self.config().raw()), encoding="utf-8")
        return str(path)


if __name__ == "__main__":
    unittest.main()
