#SPDX:MIT
"""Test what `scoter run` runs: the configuration in use, staged and checked (`run_config`)."""
import datetime
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
from surf_scoter_lib import cleanup, generator, host_file, run_config
from surf_scoter_lib.active_env import ActiveEnvironmentSet, EnvironmentDesc
from surf_scoter_lib.pull import analyze, approve, seed
from surf_scoter_lib.user_config import UserConfig
from surf_scoter_lib.verified import configs, snapshot, store

from .dist_helper import HAVE_GENERATOR, NEEDS_GENERATOR
from .test_main_pull import PROJECT_FILE, PullBase

ONLY_LS = PROJECT_FILE.replace('  - {command: sh, args: ["-c", {any: {}}]}\n', "")


@unittest.skipUnless(HAVE_GENERATOR, NEEDS_GENERATOR)
class PrepareTest(PullBase):
    """Staging and checking the configuration in use, with the real generator."""

    def setUp(self) -> None:
        super().setUp()
        (self.files / "project.yaml").write_text(ONLY_LS, encoding="utf-8")
        self.real_project = os.path.realpath(self.project)
        self.env_id = store.environment_id(Path(self.real_project))

    def pulled(self) -> approve.Approved:
        """Pull and approve the project as it is."""
        seeded = seed.seed(self.config(), generate=generator.run, path_env=self.path_env)
        report = analyze.analyze(self.config(), seeded.workdir_id, generate=generator.run, path_env=self.path_env)
        return approve.approve(
            self.config(), seeded.workdir_id, report["id"], generate=generator.run, path_env=self.path_env,
        )

    def prepare(self, config: UserConfig | None = None, now: datetime.datetime | None = None) -> run_config.Prepared:
        dest = Path(tempfile.mkdtemp(dir=self.tmp))
        os.chmod(dest, 0o700)
        return run_config.prepare(
            config or self.config(), dest, generate=generator.run, now=now, path_env=self.path_env,
        )

    def link(self) -> configs.Link:
        found = configs.load_link(store.VerifiedStore.open(self.verified), self.real_project, self.env_id)
        assert found is not None  # nosec: B101  # narrowing for the type checker
        return found

    def save_link(self, link: configs.Link) -> None:
        verified = store.VerifiedStore.open(self.verified)
        with verified.locked():
            configs.save_link(verified, link)

    def with_values(self, section: str, **values: object) -> UserConfig:
        """The found SUC, with `section`'s values changed (as a setup file would)."""
        config = self.config()
        resolved = config.raw()["resolved"]  # type: ignore[index]
        resolved.setdefault(section, {}).setdefault("values", {}).update(  # type: ignore[union-attr]
            {k.replace("_", "-"): v for k, v in values.items()},
        )
        return config

    def test_nothing_to_run(self) -> None:
        with self.assertRaisesRegex(run_config.RunRefused, "no configuration for this project"):
            self.prepare()

    def test_locked(self) -> None:
        """The configuration in use, staged; the host commands only from its host file."""
        approved = self.pulled()
        prepared = self.prepare()
        run = prepared.config
        self.assertEqual(prepared.config_id, approved.config_id)
        self.assertEqual(run.active_location("config-dir"), prepared.staged / "files")
        self.assertEqual(run.host_file_path(), prepared.staged / "host.json")
        self.assertEqual([c["program"] for c in run.raw("host-brokered")], [str(self.bin / "ls")])  # type: ignore[union-attr]
        self.assertEqual(run.raw("configuration"), {
            "environment": self.env_id, "id": approved.config_id, "unlocked": False,
        })
        self.assertEqual((prepared.notices, prepared.warnings), ([], []))
        # The gateway enforces the configuration's egress rules.
        self.assertEqual(
            [(r["host"], r["protocol"], r["port"]) for r in run.egress_rules()],
            [("registry.npmjs.org", "tls", 443)],
        )
        self.assertEqual(run.raw("egress", "gateway"), "squid")
        # The project's live files changing doesn't change what runs, but it's noticed (VER-21).
        (self.files / "env" / "Dockerfile").write_text("FROM changed\n", encoding="utf-8")
        prepared = self.prepare()
        self.assertEqual(prepared.notices, ["The project's files changed since your last pull: `scoter pull` reviews the changes."])
        self.assertEqual((prepared.staged / "files" / "env" / "Dockerfile").read_text(encoding="utf-8"), "FROM scratch\n")

    def test_program_gone(self) -> None:
        """A host file program that's gone stops the run; it's never looked for elsewhere (BRIDGE-11)."""
        self.pulled()
        os.unlink(self.bin / "ls")
        with self.assertRaisesRegex(run_config.RunRefused, "isn't an executable file any more"):
            self.prepare()

    def test_checks_every_run(self) -> None:
        """A check added to the setup since the review refuses the run, and says where it's from (VER-22)."""
        self.pulled()
        setup = self.home / "setup.yaml"
        setup.write_text(
            setup.read_text(encoding="utf-8")
            + "  host-commands:\n    no-ls:\n      action: refuse\n      reason: Not ls.\n      commands: [ls]\n",
            encoding="utf-8",
        )
        self.generate("--distribution-dir", str(self.dist), output=self.tmp / "first.json")
        with self.assertRaises(run_config.RunRefused) as caught:
            self.prepare()
        message = str(caught.exception)
        self.assertIn("Not ls.", message)
        self.assertIn("`verification:no-ls`", message)
        self.assertIn(str(setup), message)
        self.assertIn("overrides", message)

    def test_timers(self) -> None:
        """The notice, once per interval; the warning; the expiry (VER-20)."""
        self.pulled()
        reviewed = self.link().last_reviewed
        assert reviewed is not None  # nosec: B101  # narrowing for the type checker
        day = datetime.timedelta(days=1)
        self.assertEqual(self.prepare(now=reviewed + 29 * day).notices, [])
        notices = self.prepare(now=reviewed + 31 * day).notices
        self.assertIn("31 days ago", notices[0])
        self.assertTrue(any("host command: ls runs" in n for n in notices))
        self.assertTrue(any("egress: registry.npmjs.org" in n for n in notices))
        self.assertEqual(self.prepare(now=reviewed + 32 * day).notices, [])
        self.assertTrue(self.prepare(now=reviewed + 38 * day).notices)
        warned = self.prepare(self.with_values("verification", review_warn_days=10), now=reviewed + 11 * day)
        self.assertIn("11 days ago", warned.warnings[0])
        with self.assertRaisesRegex(run_config.RunRefused, "review-expire-days"):
            self.prepare(self.with_values("verification", review_expire_days=40), now=reviewed + 41 * day)

    def unlock(self) -> tuple[UserConfig, Path]:
        """Unlock the configuration in use into `unlocked/ENV-ID`, as `scoter unlock` will."""
        approved = self.pulled()
        verified = store.VerifiedStore.open(self.verified)
        in_use = configs.load_config(verified, self.env_id, approved.config_id)
        assert in_use is not None  # nosec: B101  # narrowing for the type checker
        root = self.tmp / "unlocked" / self.env_id
        (root / "files").mkdir(parents=True, mode=0o700)
        snapshot.restore(verified, in_use.snapshot, root / "files", snapshot.SnapshotLimits())
        host_file.write(root, in_use.host_file)
        link = self.link()
        link.in_use = None
        link.unlocked = configs.Unlocked(store.now_utc(), approved.config_id)
        self.save_link(link)
        config = self.config()
        config.raw()["resolved"].setdefault("pull", {}).setdefault("paths", {})["unlocked-dir"] = str(self.tmp / "unlocked")  # type: ignore[index, union-attr]
        return config, root

    def test_unlocked(self) -> None:
        """The unlocked copy runs, staged, with a warning; edits are used; others can't write it (VER-33)."""
        config, root = self.unlock()
        (root / "files" / "env" / "Dockerfile").write_text("FROM edited\n", encoding="utf-8")
        prepared = self.prepare(config)
        self.assertIsNone(prepared.config_id)
        self.assertTrue(any("unlocked configuration" in w for w in prepared.warnings))
        self.assertEqual((prepared.staged / "files" / "env" / "Dockerfile").read_text(encoding="utf-8"), "FROM edited\n")
        self.assertTrue(prepared.config.raw("configuration")["unlocked"])  # type: ignore[index]
        later = store.now_utc() + datetime.timedelta(days=8)
        self.assertTrue(any("registry.npmjs.org" in w for w in self.prepare(config, now=later).warnings))
        config.raw()["resolved"]["pull"].setdefault("values", {})["unlock-days"] = 5  # type: ignore[index]
        with self.assertRaisesRegex(run_config.RunRefused, "unlock-days"):
            self.prepare(config, now=later)
        config.raw()["resolved"]["pull"]["values"]["unlock-days"] = 0  # type: ignore[index]
        target = root / "files" / "project.yaml"
        os.chmod(target, os.stat(target).st_mode | stat.S_IWOTH)
        with self.assertRaisesRegex(run_config.RunRefused, "not yours alone"):
            self.prepare(config)

    def test_stale_unlocked_dir(self) -> None:
        """Files in the unlocked directory of a locked configuration are warned of (VER-36)."""
        self.pulled()
        config = self.config()
        config.raw()["resolved"].setdefault("pull", {}).setdefault("paths", {})["unlocked-dir"] = str(self.tmp / "unlocked")  # type: ignore[index, union-attr]
        (self.tmp / "unlocked" / self.env_id).mkdir(parents=True)
        (self.tmp / "unlocked" / self.env_id / "left.txt").write_text("x", encoding="utf-8")
        self.assertTrue(any("aren't used" in w for w in self.prepare(config).warnings))

    def test_running_configurations(self) -> None:
        """A run's record names its configuration; a retired one waits for it (VER-40)."""
        first = self.pulled()
        prepared = self.prepare()
        env = EnvironmentDesc(
            host="scoter-x", dev_env_name="", firewall_router_name="", network_gateway_name="",
            project_dir="", resources_dir="", project_clone_dir="", setup_dir="", compose_file="",
            compose_env_file="", dev_env_ssh_port="2000", dev_env_ssh_user="me",
            created_at="2026-11-03T10:00:00", suc=json.loads(prepared.config.dumps()), pid=os.getpid(),
        )
        config = self.config()
        with ActiveEnvironmentSet.locked(
            config.require_path("scoter", "running-envs"), config.require_path("scoter", "running-envs-lock"),
        ) as envs:
            envs.environs[env.host] = env
        self.assertEqual(run_config.running_config_ids(config), {first.config_id})
        # Two more pulls: the first is retired, but waits while it runs.
        for text in ("FROM scratch\nRUN a\n", "FROM scratch\nRUN b\n"):
            (self.files / "env" / "Dockerfile").write_text(text, encoding="utf-8")
            seeded = seed.seed(self.config(), generate=generator.run, path_env=self.path_env)
            report = analyze.analyze(self.config(), seeded.workdir_id, generate=generator.run, path_env=self.path_env)
            approve.approve(
                self.config(), seeded.workdir_id, report["id"], generate=generator.run, path_env=self.path_env,
                running=run_config.running_config_ids(config),
            )
        self.assertEqual(self.link().removing, [first.config_id])
        # The staged copy it runs from isn't left over, while it runs.
        orphaned = cleanup._orphaned_state(
            self.state, ActiveEnvironmentSet.read(config.require_path("scoter", "running-envs")),
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=2),
        )
        self.assertNotIn(prepared.staged, orphaned)
        with ActiveEnvironmentSet.locked(
            config.require_path("scoter", "running-envs"), config.require_path("scoter", "running-envs-lock"),
        ) as envs:
            envs.rm_environ_by_host(env.host)
        # The next run removes it.
        self.prepare()
        self.assertEqual(self.link().removing, [])
        names = {c.name for c in configs.list_configs(store.VerifiedStore.open(self.verified), self.env_id)}
        self.assertEqual(names, {"default", "default.previous"})
        shutil.rmtree(prepared.staged)
