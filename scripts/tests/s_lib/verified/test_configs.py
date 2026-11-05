#SPDX:MIT
"""Test the verified configs module: locked configurations and links."""
import datetime
import unittest
from dataclasses import replace

from ....surf_scoter_lib import err
from ....surf_scoter_lib.verified import configs, snapshot
from .helper import POSIX, TempDirTest

NOW = datetime.datetime(2026, 11, 3, 12, 0, tzinfo=datetime.UTC)


class ConfigsJsonTest(unittest.TestCase):
    """Test the records' JSON and rules, outside the store."""

    def test_names_and_ids(self) -> None:
        """Names are plain; ids are as `new_config_id` makes them."""
        for name in ("default", "Team.2", "a_b-c", "x" * 64):
            configs.check_name(name)
        for name in ("", "-x", ".x", "a/b", "a b", "café", "x" * 65, "a\x1b"):
            with self.subTest(name=ascii(name)), self.assertRaises(err.VerifiedStoreErr):
                configs.check_name(name)
        configs.check_id(configs.new_config_id(NOW))
        for config_id in ("", "../x", "20261103T120000Z", "default"):
            with self.subTest(config_id=config_id), self.assertRaises(err.VerifiedStoreErr):
                configs.check_id(config_id)

    def test_config_json(self) -> None:
        """A configuration survives JSON, and its content id ignores its name and time."""
        manifest = snapshot.Manifest({"a": snapshot.FileEntry(sha256="0" * 64, size=1, executable=False)})
        config = configs.LockedConfig(
            configs.new_config_id(NOW), "default", "env-1", NOW, manifest, {"commands": {}},
        )
        self.assertEqual(configs.LockedConfig.from_json(config.to_json()), config)
        other = replace(config, id=configs.new_config_id(NOW), name="other", created_at=NOW.replace(year=2027))
        self.assertEqual(other.content_id, config.content_id)
        self.assertNotEqual(replace(config, host_file={"commands": {"x": 1}}).content_id, config.content_id)
        bad = config.to_json()
        bad["snapshot"]["id"] = "sha256:other"
        with self.assertRaisesRegex(ValueError, "doesn't match"):
            configs.LockedConfig.from_json(bad)

    def test_link_json(self) -> None:
        """A link survives JSON; one in an impossible state is refused."""
        used, unlocked_from = configs.new_config_id(NOW), configs.new_config_id(NOW)
        link = configs.Link("env-1", "/p", in_use=used, last_reviewed=NOW, provenance={"source": "x"})
        self.assertEqual(configs.Link.from_json(link.to_json()), link)
        link = configs.Link("env-1", "/p", unlocked=configs.Unlocked(NOW, unlocked_from, "base"))
        self.assertEqual(configs.Link.from_json(link.to_json()), link)
        self.assertEqual(link.kept(), {unlocked_from})
        both = configs.Link("env-1", "/p", in_use=used, unlocked=configs.Unlocked(NOW)).to_json()
        with self.assertRaisesRegex(ValueError, "unlocked"):
            configs.Link.from_json(both)
        marked = configs.Link("env-1", "/p", in_use=used, removing=[used]).to_json()
        with self.assertRaisesRegex(ValueError, "removing"):
            configs.Link.from_json(marked)

    def test_reviewed(self) -> None:
        """A review starts the timers again."""
        link = configs.Link("env-1", "/p", last_reviewed=NOW, last_notice=NOW)
        later = NOW + datetime.timedelta(days=40)
        link.reviewed(later)
        self.assertEqual((link.last_reviewed, link.last_notice), (later, None))


@unittest.skipUnless(POSIX, "owner and mode checks")
class ConfigsStoreTest(TempDirTest):
    """Test the records in the store."""

    def setUp(self) -> None:
        super().setUp()
        self.store = self.new_store()

    def taken(self, text: str) -> snapshot.Snapshot:
        return snapshot.snapshot_bytes([("project.yaml", text.encode(), False)], snapshot.SnapshotLimits())

    def new_config(self, link: configs.Link, text: str, name: str = "default", seconds: int = 0) -> configs.LockedConfig:
        """A configuration of `link`'s environment holding a project file `text`, its blobs stored."""
        taken = self.taken(text)
        at = NOW + datetime.timedelta(seconds=seconds)
        with self.store.locked():
            taken.store(self.store)
        return configs.LockedConfig(configs.new_config_id(at), name, link.id, at, taken.manifest, {})

    def test_save_and_load(self) -> None:
        """Saved, listed newest first, found by name, and cross-checked on loading."""
        link = configs.Link("env-1", "/p")
        first = self.new_config(link, "a: 1\n")
        second = self.new_config(link, "a: 2\n", "other", seconds=1)
        with self.store.locked():
            configs.save_config(self.store, link, first)
            configs.save_config(self.store, link, second)
            link.in_use = first.id
            configs.save_link(self.store, link)
        self.assertEqual(self.store.index(), {"/p": "env-1"})
        self.assertEqual(configs.load_link(self.store, "/p", "env-1"), link)
        self.assertEqual(configs.list_configs(self.store, "env-1"), [second, first])
        self.assertEqual(configs.find_config(self.store, link, "other"), second)
        self.assertIsNone(configs.find_config(self.store, link, "missing"))
        self.assertIsNone(configs.load_link(self.store, "/p", "env-2"))
        with self.assertRaisesRegex(err.VerifiedStoreErr, "is for /p"):
            configs.load_link(self.store, "/q", "env-1")
        # Another environment's configuration, copied in, is refused.
        with self.store.locked():
            self.store.write_json("env-2", configs.CONFIGS_DIR, first.id + ".json", data=first.to_json())
        with self.assertRaisesRegex(err.VerifiedStoreErr, "of `env-1`"):
            configs.load_config(self.store, "env-2", first.id)

    def test_save_refused(self) -> None:
        """No overwriting, no taken name, no missing blob, no link to nothing."""
        link = configs.Link("env-1", "/p")
        first = self.new_config(link, "a: 1\n")
        same_name = self.new_config(link, "a: 2\n", seconds=1)
        with self.store.locked():
            configs.save_config(self.store, link, first)
            with self.assertRaisesRegex(err.VerifiedStoreErr, "already has a configuration `"):
                configs.save_config(self.store, link, first)
            with self.assertRaisesRegex(err.VerifiedStoreErr, "named 'default'"):
                configs.save_config(self.store, link, same_name)
            # Replacing it, the name may be used again.
            configs.save_config(self.store, link, same_name, replacing=first.id)
            missing = configs.LockedConfig(
                configs.new_config_id(NOW), "x", link.id, NOW, self.taken("never stored\n").manifest, {},
            )
            with self.assertRaisesRegex(err.VerifiedStoreErr, "missing"):
                configs.save_config(self.store, link, missing)
            with self.assertRaisesRegex(err.VerifiedStoreErr, "is for `env-1`"):
                configs.save_config(self.store, configs.Link("env-2", "/q"), replace(first, id=configs.new_config_id(NOW)))
            link.in_use = configs.new_config_id(NOW)
            with self.assertRaisesRegex(err.VerifiedStoreErr, "isn't stored"):
                configs.save_link(self.store, link)

    def test_retire(self) -> None:
        """A replaced configuration goes, unless a running environment uses it (VER-40)."""
        link = configs.Link("env-1", "/p")
        old = self.new_config(link, "a: 1\n")
        new = self.new_config(link, "a: 2\n", seconds=1)
        with self.store.locked():
            configs.save_config(self.store, link, old)
            configs.save_config(self.store, link, new, replacing=old.id)
            link.in_use = new.id
            with self.assertRaisesRegex(err.VerifiedStoreErr, "still in use"):
                configs.retire(self.store, link, new.id, ())
            self.assertFalse(configs.retire(self.store, link, old.id, {old.id}))
            configs.save_link(self.store, link)
        self.assertEqual(configs.load_link(self.store, "/p", "env-1").removing, [old.id])
        # Waiting for removal, its name is free, and it isn't found by it.
        self.assertEqual(configs.find_config(self.store, link, "default"), new)
        with self.store.locked():
            self.assertEqual(configs.remove_retired(self.store, link, {old.id}), [])
            self.assertEqual(configs.remove_retired(self.store, link, ()), [old.id])
            configs.save_link(self.store, link)
        self.assertEqual(configs.list_configs(self.store, "env-1"), [new])
        self.assertEqual(link.removing, [])

    def test_blobs(self) -> None:
        """Blob collection keeps what configurations and bases use."""
        link = configs.Link("env-1", "/p")
        config = self.new_config(link, "in a configuration\n")
        base = self.taken("the base\n")
        unused = self.taken("nobody\n")
        with self.store.locked():
            base.store(self.store)
            unused.store(self.store)
            configs.save_config(self.store, link, config)
            link.in_use = config.id
            link.base = base.manifest
            configs.save_link(self.store, link)
            self.assertEqual(configs.remove_unused_blobs(self.store), 1)
        self.assertEqual(
            configs.used_blobs(self.store), config.blobs() | link.blobs(),
        )
        restored = self.tmp / "restored"
        restored.mkdir(mode=0o700)
        snapshot.restore(self.store, config.snapshot, restored, snapshot.SnapshotLimits())
        self.assertEqual((restored / "project.yaml").read_text(), "in a configuration\n")
