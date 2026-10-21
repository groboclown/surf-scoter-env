#SPDX:MIT
"""Test the verified accept module."""
import datetime
import json
import os
import unittest

from ....surf_scoter_lib import jdata
from ....surf_scoter_lib.err import VerifiedStoreErr
from ....surf_scoter_lib.user_config import UserConfig
from ....surf_scoter_lib.verified import accept, lookup, records, store
from .helper import POSIX, TempDirTest


@unittest.skipUnless(POSIX, "owner and mode checks")
class StoreAcceptanceTest(TempDirTest):
    """Test storing a review's acceptance."""

    def setUp(self) -> None:
        super().setUp()
        self.project = self.tmp / "proj"
        self.write("proj/.surf-scoter.d/project.yaml", "container: {dev-container-path: env}\n")
        self.write("proj/.surf-scoter.d/env/Dockerfile", "FROM scratch\n")
        self.verified = self.tmp / "verified"

    def suc(self) -> jdata.DictJsonData:
        return {
            "active-config-locations": {
                "project-dir": str(self.project),
                "dev-container-path": str(self.project / ".surf-scoter.d" / "env"),
            },
            "resolved": {
                "verification": {
                    "paths": {"store": str(self.verified)},
                    "path-lists": {"verified-containers": []},
                },
                "scoter": {
                    "path-lists": {"dev-containers-search": []},
                    "values": {
                        "project-files-dir": ".surf-scoter.d",
                        "project-file-name": "project.yaml",
                    },
                },
            },
            "current-project-setup": {"container": {"dev-container-path": "env"}},
            "dev-container": {"user": "me", "parent-builds": []},
            "setup": {"files": []},
        }

    def store(self, config: UserConfig, expires: datetime.datetime) -> records.Acceptance:
        accepted: jdata.DictJsonData = {"expires-at": expires.isoformat(), "commands": []}
        return accept.store_acceptance(
            config, lookup.project_snapshot(config, self.project), b"review: x\n",
            accepted, "main", records.now_utc() - datetime.timedelta(seconds=1),
        )

    def test_stored(self) -> None:
        """A new store, its files private, and the lookup finding it."""
        config = UserConfig(self.suc())
        expires = records.now_utc() + datetime.timedelta(days=3)
        acceptance = self.store(config, expires)
        self.assertEqual(acceptance.expires_at, expires)
        self.assertEqual(os.stat(self.verified).st_mode & 0o777, 0o700)
        found = lookup.find(config)
        self.assertEqual((found.match, found.acceptance), (records.MatchKind.EXACT, acceptance.id))
        where = self.verified / store.environment_id(self.project) / records.ACCEPTANCES_DIR / acceptance.id
        self.assertEqual((where / accept.REVIEW_FILE).read_bytes(), b"review: x\n")
        self.assertEqual(json.loads((where / lookup.ACCEPTED_FILE).read_text())["commands"], [])
        suggestion = json.loads((where / accept.SUGGESTION_FILE).read_text())
        self.assertEqual(suggestion["dev-container"]["user"], "me")
        for name in (accept.REVIEW_FILE, lookup.ACCEPTED_FILE, accept.SUGGESTION_FILE):
            self.assertEqual(os.stat(where / name).st_mode & 0o777, 0o600)

    def test_replaces_the_branchs(self) -> None:
        """Each acceptance replaces the branch's earlier one, and its files go."""
        config = UserConfig(self.suc())
        old = self.store(config, records.now_utc() + datetime.timedelta(days=1))
        newest = self.store(config, records.now_utc() + datetime.timedelta(days=3))
        verified_store = store.VerifiedStore.open(self.verified)
        env_id = store.environment_id(self.project)
        record = records.load_environment(verified_store, str(self.project), env_id)
        assert record is not None  # nosec: B101  # narrowing for the type checker
        self.assertEqual([a.id for a in record.acceptances], [newest.id])
        self.assertFalse((self.verified / env_id / records.ACCEPTANCES_DIR / old.id).exists())

    def test_no_store_named(self) -> None:
        """Without `verification: paths: store`, nothing can be stored."""
        suc = self.suc()
        del suc["resolved"]["verification"]["paths"]  # type: ignore[union-attr]
        with self.assertRaisesRegex(VerifiedStoreErr, "verification: paths: store"):
            self.store(UserConfig(suc), records.now_utc() + datetime.timedelta(days=1))
