#SPDX:MIT
"""Test the verified records module."""
import datetime
import unittest

from ....surf_scoter_lib import err
from ....surf_scoter_lib.user_config import UserConfig
from ....surf_scoter_lib.verified import records, snapshot
from .helper import POSIX, TempDirTest


def _manifest(text: str) -> snapshot.Manifest:
    return snapshot.Manifest({
        "f": snapshot.FileEntry(sha256=text * 64, size=1, executable=False, canonical="sha256:same"),
    })


class RecordsTest(TempDirTest):
    """Test the acceptance records."""

    NOW = datetime.datetime(2026, 10, 20, 12, 0, tzinfo=datetime.UTC)
    SETUP = records.SetupRecord({"a": 1})

    def acceptance(
        self, manifest: snapshot.Manifest | None, *, days: int = 30, age: int = 0,
        kind: records.Kind = records.Kind.REVIEWED,
    ) -> records.Acceptance:
        accepted = self.NOW - datetime.timedelta(days=age)
        return records.Acceptance(
            id=records.new_acceptance_id(accepted),
            kind=kind,
            accepted_at=accepted,
            expires_at=accepted + datetime.timedelta(days=days),
            setup=self.SETUP,
            suggestion=manifest,
        )

    def found(self, match: records.Match | None) -> records.Match:
        if match is None:
            self.fail("no acceptance matched")
        return match

    def first_acceptance(self, data: dict[str, object]) -> dict[str, object]:
        acceptances = data["acceptances"]
        if not isinstance(acceptances, list) or not isinstance(acceptances[0], dict):
            self.fail("acceptances: expected a list of mappings")
        return acceptances[0]

    def test_validity(self) -> None:
        """Valid, expired, exempt, and invalid windows."""
        acceptance = self.acceptance(_manifest("0"))
        self.assertEqual(acceptance.validity(self.NOW), records.Validity.VALID)
        later = self.NOW + datetime.timedelta(days=31)
        self.assertEqual(acceptance.validity(later), records.Validity.EXPIRED)
        earlier = self.NOW - datetime.timedelta(seconds=1)
        self.assertEqual(acceptance.validity(earlier), records.Validity.INVALID)
        exempt = records.Acceptance(**{
            **acceptance.__dict__, "exempt_until": self.NOW + datetime.timedelta(days=1),
        })
        self.assertEqual(exempt.validity(self.NOW), records.Validity.EXEMPT)

    def test_find(self) -> None:
        """Exact, then on meaning, then blanket; only usable ones, under the same setup."""
        old = _manifest("0")
        now = _manifest("1")
        env = records.EnvironmentRecord("e", "/p", [])
        self.assertIsNone(env.find(now, self.SETUP, self.NOW))
        env.add(self.acceptance(old, age=1))
        found = self.found(env.find(now, self.SETUP, self.NOW))
        self.assertEqual(found.kind, records.MatchKind.MEANING)
        self.assertIsNone(env.find(now, records.SetupRecord({"a": 2}), self.NOW))
        exact = self.acceptance(now, age=2)
        env.add(exact)
        found = self.found(env.find(now, self.SETUP, self.NOW))
        self.assertEqual((found.kind, found.acceptance), (records.MatchKind.EXACT, exact))
        other = snapshot.Manifest({"g": snapshot.FileEntry("2" * 64, 1, False)})
        self.assertIsNone(env.find(other, self.SETUP, self.NOW))
        env.add(self.acceptance(None, kind=records.Kind.BLANKET))
        found = self.found(env.find(other, self.SETUP, self.NOW))
        self.assertEqual(found.kind, records.MatchKind.BLANKET)
        # Expired ones don't count.
        later = self.NOW + datetime.timedelta(days=40)
        self.assertIsNone(env.find(now, self.SETUP, later))

    def test_carry_forward(self) -> None:
        """Carrying forward keeps the earlier acceptance's expiry."""
        env = records.EnvironmentRecord("e", "/p", [])
        earlier = self.acceptance(_manifest("0"), age=10)
        env.add(earlier)
        current = _manifest("1")
        found = self.found(env.find(current, self.SETUP, self.NOW))
        carried = env.carry_forward(found, current, self.NOW, "main")
        self.assertEqual(carried.kind, records.Kind.CARRIED)
        self.assertEqual(carried.from_id, earlier.id)
        self.assertEqual(carried.expires_at, earlier.expires_at)
        self.assertEqual(carried.suggestion, current)
        self.assertEqual(env.acceptances[0], carried)
        exact = self.found(env.find(current, self.SETUP, self.NOW))
        with self.assertRaises(ValueError):
            env.carry_forward(exact, current, self.NOW, None)

    def test_prune(self) -> None:
        """Expired acceptances beyond the newest `keep` go; usable ones stay."""
        env = records.EnvironmentRecord("e", "/p", [])
        for age in (100, 90, 80, 0):
            env.add(self.acceptance(_manifest(str(age % 10)), age=age))
        removed = env.prune(2, self.NOW)
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0].accepted_at, self.NOW - datetime.timedelta(days=100))
        self.assertEqual(len(env.acceptances), 3)

    def test_json(self) -> None:
        """Records survive JSON; a blanket acceptance has no suggestion, others must."""
        env = records.EnvironmentRecord("e", "/p", [
            self.acceptance(_manifest("0")),
            self.acceptance(None, kind=records.Kind.BLANKET),
        ])
        self.assertEqual(records.EnvironmentRecord.from_json(env.to_json()), env)
        data = env.to_json()
        self.first_acceptance(data)["suggestion"] = None
        with self.assertRaisesRegex(ValueError, "only a blanket"):
            records.EnvironmentRecord.from_json(data)
        bad = env.to_json()
        self.first_acceptance(bad)["setup"] = {"id": "sha256:x", "values": {"a": 1}}
        with self.assertRaisesRegex(ValueError, "setup.id"):
            records.EnvironmentRecord.from_json(bad)

    @unittest.skipUnless(POSIX, "owner and mode checks")
    def test_in_the_store(self) -> None:
        """Saving registers, prunes, and removes unused blobs; loading cross-checks."""
        verified = self.new_store()
        files = self.tmp / "proj"
        self.write("proj/project.yaml", "a: 1\n")
        taken = snapshot.snapshot_project(
            files, "project.yaml", [], snapshot.ParsedFiles(), snapshot.SnapshotLimits(),
        )
        self.assertIsNone(records.load_environment(verified, "/p", "e"))
        old = self.acceptance(taken.manifest, age=100)
        with verified.locked():
            taken.store(verified)
            env = records.EnvironmentRecord("e", "/p", [old])
            records.save_environment(verified, env)
        loaded = records.load_environment(verified, "/p", "e")
        self.assertEqual(loaded, env)
        with self.assertRaisesRegex(err.VerifiedStoreErr, "is for /p"):
            records.load_environment(verified, "/q", "e")
        self.assertEqual(verified.blob_size(), 5)
        with verified.locked():
            removed = records.prune_environment(verified, env, keep=0)
        self.assertEqual(removed, [old])
        self.assertEqual(verified.blob_size(), 0)

    def test_setup_values(self) -> None:
        """The setup boundaries, from the SUC."""
        suc = {
            "resolved": {
                "scoter": {
                    "paths": {"verified-dir": "/v"},
                    "path-lists": {"dev-containers-search": ["/a"], "dev-containers-verified": []},
                    "values": {"project-files-dir": ".surf-scoter.d"},
                },
                "git-vcs": {"runs": {"local-clone": ["git", "clone"], "find-root": ["x"]}},
            },
            "host-command-checks": {"checks": {"h:s": {"action": "refuse"}}},
            "setup": {"files": [
                {"file": "/u", "sections": {"h": {"host-commands": {"s": {"action": "warn"}}}}},
                {"file": "/d", "sections": {"h": {"host-commands": {"s": {"action": "refuse"}}}}},
            ]},
        }
        values = records.setup_values(UserConfig(suc), "git-vcs", {"/c": "sha256:c"})
        self.assertEqual(values["verified-dir"], "/v")
        self.assertEqual(values["vcs-runs"], {
            "local-clone": ["git", "clone"], "fetch-changes-into": None,
            "show-fetch-changes": None, "merge-changes": None,
        })
        self.assertEqual(values["host-command-checks"], {"h:s": {"action": "refuse"}})
        self.assertEqual(
            values["hidden-host-commands"],
            {"h:s": [{"file": "/d", "check": {"action": "refuse"}}]},
        )
        self.assertEqual(values["verified-containers"], {"/c": "sha256:c"})
