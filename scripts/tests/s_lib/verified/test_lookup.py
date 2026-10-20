#SPDX:MIT
"""Test the verified lookup module."""
import datetime
import json
import unittest

from ....surf_scoter_lib import jdata
from ....surf_scoter_lib.user_config import UserConfig
from ....surf_scoter_lib.verified import lookup, records, snapshot, store
from .helper import POSIX, TempDirTest


@unittest.skipUnless(POSIX, "owner and mode checks")
class LookupTest(TempDirTest):
    """Test finding an acceptance from a universal configuration file."""

    def setUp(self) -> None:
        super().setUp()
        self.project = self.tmp / "proj"
        self.write("proj/.surf-scoter.d/project.yaml", "container: {dev-container-path: env}\n")
        self.write("proj/.surf-scoter.d/env/Dockerfile", "FROM scratch\n")
        self.write("proj/.surf-scoter.d/env/dev-env.yaml", "user: me\n")
        self.verified = self.new_store()

    def suc(self) -> jdata.DictJsonData:
        return {
            "active-config-locations": {
                "project-dir": str(self.project),
                "dev-container-path": str(self.project / ".surf-scoter.d" / "env"),
            },
            "resolved": {
                "scoter": {
                    "paths": {"verified-dir": str(self.verified.root)},
                    "path-lists": {"dev-containers-search": [], "dev-containers-verified": []},
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

    def accept(self, kind: records.Kind = records.Kind.REVIEWED) -> records.Acceptance:
        """Accept the project's files as they are now."""
        config = UserConfig(self.suc())
        taken = lookup._snapshot(config, self.project)
        setup = records.SetupRecord(records.setup_values(config, None, {}))
        now = records.now_utc()
        acceptance = records.Acceptance(
            id=records.new_acceptance_id(now - datetime.timedelta(seconds=1)),
            kind=kind,
            accepted_at=now - datetime.timedelta(seconds=1),
            expires_at=now + datetime.timedelta(days=30),
            setup=setup,
            suggestion=None if kind == records.Kind.BLANKET else taken.manifest,
        )
        env_id = store.environment_id(self.project)
        with self.verified.locked():
            taken.store(self.verified)
            record = records.EnvironmentRecord(env_id, str(self.project), [acceptance])
            if kind != records.Kind.BLANKET:
                self.verified.write_bytes(
                    env_id, records.ACCEPTANCES_DIR, acceptance.id, lookup.ACCEPTED_FILE,
                    data=json.dumps({"commands": []}).encode(),
                )
            records.save_environment(self.verified, record, keep=10)
        return acceptance

    def test_nothing_accepted(self) -> None:
        """No store, or no record, is no match."""
        self.assertEqual(lookup.find(UserConfig(self.suc())), lookup.Lookup())
        missing = self.suc()
        paths = missing["resolved"]["scoter"]["paths"]  # type: ignore[index]
        paths["verified-dir"] = str(self.tmp / "nowhere")  # type: ignore[index]
        self.assertEqual(lookup.find(UserConfig(missing)), lookup.Lookup())
        self.assertEqual(lookup.find(UserConfig({})), lookup.Lookup())

    def test_exact(self) -> None:
        """The files as accepted: the acceptance's own `accepted.json`."""
        acceptance = self.accept()
        found = lookup.find(UserConfig(self.suc()))
        self.assertEqual(found.match, records.MatchKind.EXACT)
        self.assertEqual(found.acceptance, acceptance.id)
        assert found.accepted_settings is not None  # nosec: B101  # narrowing for the type checker
        self.assertTrue(found.accepted_settings.is_file())
        self.assertIsNone(found.notice)
        self.assertEqual(found.to_json()["match"], "exact")

    def test_meaning_carries_forward(self) -> None:
        """A comment changes nothing that matters: the acceptance is carried forward."""
        original = self.accept()
        self.write("proj/.surf-scoter.d/env/Dockerfile", "# A new comment.\nFROM scratch\n")
        found = lookup.find(UserConfig(self.suc()))
        self.assertEqual(found.match, records.MatchKind.MEANING)
        self.assertNotEqual(found.acceptance, original.id)
        self.assertIsNotNone(found.notice)
        env_id = store.environment_id(self.project)
        record = records.load_environment(self.verified, str(self.project), env_id)
        assert record is not None  # nosec: B101  # narrowing for the type checker
        carried = record.acceptances[0]
        self.assertEqual((carried.kind, carried.from_id), (records.Kind.CARRIED, original.id))
        self.assertEqual(carried.expires_at, original.expires_at)
        assert found.accepted_settings is not None  # nosec: B101  # narrowing for the type checker
        self.assertEqual(found.accepted_settings.read_text(), json.dumps({"commands": []}))
        # Now it matches exactly.
        self.assertEqual(lookup.find(UserConfig(self.suc())).match, records.MatchKind.EXACT)

    def test_changed(self) -> None:
        """A change that matters is no match."""
        self.accept()
        self.write("proj/.surf-scoter.d/env/Dockerfile", "FROM ubuntu\n")
        self.assertEqual(lookup.find(UserConfig(self.suc())), lookup.Lookup())

    def test_blanket(self) -> None:
        """A blanket acceptance matches, without an `accepted.json`."""
        self.accept(records.Kind.BLANKET)
        self.write("proj/.surf-scoter.d/env/Dockerfile", "FROM ubuntu\n")
        found = lookup.find(UserConfig(self.suc()))
        self.assertEqual(found.match, records.MatchKind.BLANKET)
        self.assertIsNone(found.accepted_settings)

    def test_verified_containers(self) -> None:
        """Containers in `dev-containers-verified` go into the setup values; others don't."""
        suc = self.suc()
        outside = self.tmp / "dist" / "base"
        self.write("dist/base/Dockerfile", "FROM scratch\n")
        suc["dev-container"] = {"parent-builds": [{"dir": str(outside), "tag": "base"}]}
        self.assertEqual(lookup._verified_containers(UserConfig(suc)), {})
        lists = suc["resolved"]["scoter"]["path-lists"]  # type: ignore[index]
        lists["dev-containers-verified"] = [str(self.tmp / "dist")]  # type: ignore[index]
        found = lookup._verified_containers(UserConfig(suc))
        self.assertEqual(list(found), [str(outside)])
        self.assertEqual(
            found[str(outside)],
            snapshot.snapshot_dir(outside, snapshot.SnapshotLimits()).manifest.id,
        )
