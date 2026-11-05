#SPDX:MIT
"""Test the main_pull script, and the pull library: seed, analyze, changed and approve."""
import contextlib
import copy
import io
import json
import os
import shutil
import subprocess  # nosec: B404  # Makes the tests' Git repository.
import sys
import unittest
from pathlib import Path

# The scripts import the library as their own directory's, as they do when
# they run.
SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
import main_pull
from surf_scoter_lib import generator, subcommand
from surf_scoter_lib.pull import analyze, approve, seed, workdir
from surf_scoter_lib.user_config import UserConfig
from surf_scoter_lib.verified import configs, store

from .dist_helper import HAVE_GENERATOR, NEEDS_GENERATOR, DistTest

GIT = shutil.which("git")
PROJECT_FILE = """\
container:
  dev-container-path: ./env
egress:
  - {host: registry.npmjs.org, protocol: tls}
commands:
  - {command: ls, args: [{any: {}}]}
  - {command: sh, args: ["-c", {any: {}}]}
"""


class PullBase(DistTest):
    """A scratch distribution and project, with a state directory, a store and a `tools` directory."""

    def setUp(self) -> None:
        super().setUp()
        self.state = self.tmp / "state"
        (self.home / "setup.yaml").write_text(
            "scoter:\n"
            f"  paths:\n    state-dir: {json.dumps(str(self.state))}\n"
            f"  path-lists:\n    dev-containers-search:\n      - {json.dumps(str(self.mine))}\n"
            f"      - {json.dumps(str(self.dist / 'containers'))}\n"
            "verification:\n"
            f"  paths:\n    store: {json.dumps(str(self.verified))}\n",
            encoding="utf-8",
        )
        self.generate("--distribution-dir", str(self.dist), output=self.tmp / "first.json")
        self.bin = self.tmp / "tools"
        self.bin.mkdir()
        tool = self.bin / "ls"
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(tool, 0o700)
        self.path_env = str(self.bin)

    def config(self) -> UserConfig:
        """The SUC `scoter` gives `pull`: the setup, and the project found, unparsed."""
        suc = json.loads((self.tmp / "first.json").read_text(encoding="utf-8"))
        suc["found-project"] = {
            "project-dir": str(self.project), "vcs-root": None, "vcs": None, "project-branch": None,
        }
        return UserConfig(suc)

    def seed(self, **options: object) -> seed.Seeded:
        return seed.seed(self.config(), generate=generator.run, path_env=self.path_env, **options)  # type: ignore[arg-type]

    def analyze(self, workdir_id: str) -> dict:
        return analyze.analyze(self.config(), workdir_id, generate=generator.run, path_env=self.path_env)

    def approve(self, workdir_id: str, analysis_id: str, **options: object) -> approve.Approved:
        return approve.approve(
            self.config(), workdir_id, analysis_id, generate=generator.run, path_env=self.path_env, **options,  # type: ignore[arg-type]
        )

    def link(self) -> configs.Link:
        verified = store.VerifiedStore.open(self.verified)
        project = os.path.realpath(self.project)
        found = configs.load_link(verified, project, store.environment_id(Path(project)))
        assert found is not None  # nosec: B101  # narrowing for the type checker
        return found

    def drop_shell(self, files: Path) -> None:
        """Edit the working directory: take out the `sh` command the setup refuses."""
        text = (files / "project.yaml").read_text(encoding="utf-8")
        (files / "project.yaml").write_text(text.replace('  - {command: sh, args: ["-c", {any: {}}]}\n', ""), encoding="utf-8")


@unittest.skipUnless(HAVE_GENERATOR, NEEDS_GENERATOR)
class PullTest(PullBase):
    """The phases, on a scratch distribution and project, with the real generator."""

    def test_the_loop(self) -> None:
        """Seed, analyze, edit, notice the change, analyze again, approve."""
        seeded = self.seed()
        self.assertFalse(seeded.unchanged)
        self.assertTrue(any("working tree" in w for w in seeded.warnings))
        files = seeded.path / workdir.FILES_DIR
        self.assertEqual(sorted(os.listdir(files)), ["env", "project.yaml"])
        self.assertTrue((seeded.path / "host.json").is_file())
        self.assertFalse((files / "host.json").exists())
        # The provenance isn't in the working directory.
        self.assertFalse(any(p.name == workdir.PROVENANCE_FILE for p in seeded.path.rglob("*")))

        report = self.analyze(seeded.workdir_id)
        self.assertEqual(report["$schema"], analyze.REPORT_SCHEMA)
        self.assertGreater(report["root"]["counts"]["refuse"], 0)
        sh = next(c for c in report["host-commands"] if c["command"]["text"] == "sh")
        self.assertTrue(sh["findings"])
        ids = {f["id"] for f in report["findings"]}
        self.assertEqual(len(ids), len(report["findings"]))
        # The same files, analyzed again, give the same finding ids.
        self.assertEqual({f["id"] for f in self.analyze(seeded.workdir_id)["findings"]}, ids)
        with self.assertRaisesRegex(workdir.PullErr, "the checks refuse it"):
            self.approve(seeded.workdir_id, report["id"])

        self.drop_shell(files)
        changed = analyze.changed(self.config(), seeded.workdir_id, report["id"])
        self.assertEqual(changed, {"added": [], "removed": [], "changed": ["project.yaml"], "host-file": False})
        with self.assertRaisesRegex(workdir.PullErr, "changed since analysis"):
            self.approve(seeded.workdir_id, report["id"])
        again = self.analyze(seeded.workdir_id)
        self.assertEqual(again["root"]["counts"]["refuse"], 0, again["findings"])
        # The ls command's findings carry over; the shell's are gone.
        self.assertTrue({f["id"] for f in again["findings"]} < ids)
        self.assertEqual(analyze.changed(self.config(), seeded.workdir_id, again["id"])["changed"], [])

        approved = self.approve(seeded.workdir_id, again["id"])
        self.assertEqual((approved.name, approved.previous), ("default", None))
        link = self.link()
        self.assertEqual(link.in_use, approved.config_id)
        self.assertIsNotNone(link.last_reviewed)
        assert link.base is not None  # nosec: B101  # narrowing for the type checker
        provenance = workdir.open_workdir(self.config(), seeded.workdir_id).provenance
        self.assertEqual(link.base.id, provenance.seeded.id)
        stored = configs.load_config(store.VerifiedStore.open(self.verified), link.id, approved.config_id)
        assert stored is not None  # nosec: B101  # narrowing for the type checker
        self.assertEqual(stored.host_file["programs"]["ls"]["path"], str(self.bin / "ls"))
        self.assertNotIn("sh", stored.host_file["programs"])

    def test_unchanged_and_previous(self) -> None:
        """A pull of the same files is unchanged; a new approval keeps one previous configuration."""
        (self.files / "project.yaml").write_text(PROJECT_FILE.replace('  - {command: sh, args: ["-c", {any: {}}]}\n', ""), encoding="utf-8")

        def pull() -> tuple[seed.Seeded, approve.Approved]:
            seeded = self.seed()
            return seeded, self.approve(seeded.workdir_id, self.analyze(seeded.workdir_id)["id"])

        _, first = pull()
        self.assertTrue(self.seed().unchanged)
        (self.files / "env" / "Dockerfile").write_text("FROM scratch\nRUN true\n", encoding="utf-8")
        seeded = self.seed()
        self.assertFalse(seeded.unchanged)
        provenance = workdir.open_workdir(self.config(), seeded.workdir_id).provenance
        self.assertEqual(provenance.since_base, ["env/Dockerfile"])
        second = self.approve(seeded.workdir_id, self.analyze(seeded.workdir_id)["id"])
        self.assertEqual(second.previous, "default.previous")
        verified = store.VerifiedStore.open(self.verified)
        link = self.link()
        self.assertEqual(configs.find_config(verified, link, "default.previous").id, first.config_id)  # type: ignore[union-attr]
        _, third = pull()
        names = {c.name: c.id for c in configs.list_configs(verified, link.id)}
        # The first went: a name keeps one step of history.
        self.assertEqual(names, {"default": third.config_id, "default.previous": second.config_id})

    def test_setup_must_match(self) -> None:
        """A phase given a SUC with other checks than at seeding refuses the id (VER-29)."""
        seeded = self.seed()
        other = copy.deepcopy(self.config().raw())
        other["host-command-checks"]["checks"]["extra"] = {"action": "warn"}
        with self.assertRaisesRegex(workdir.PullErr, "different setup"):
            analyze.analyze(UserConfig(other), seeded.workdir_id, generate=generator.run)
        for bad in ("../x", "work", "A" * 8):
            with self.subTest(bad=bad), self.assertRaises(workdir.PullErr):
                workdir.open_workdir(self.config(), bad)

    def test_host_file_bound(self) -> None:
        """An edit to the host file after the analysis stops the approval too."""
        (self.files / "project.yaml").write_text(PROJECT_FILE.replace('  - {command: sh, args: ["-c", {any: {}}]}\n', ""), encoding="utf-8")
        seeded = self.seed()
        report = self.analyze(seeded.workdir_id)
        host = json.loads((seeded.path / "host.json").read_text(encoding="utf-8"))
        host["programs"]["ls"]["origin"] = "user"
        (seeded.path / "host.json").write_text(json.dumps(host), encoding="utf-8")
        self.assertTrue(analyze.changed(self.config(), seeded.workdir_id, report["id"])["host-file"])
        with self.assertRaisesRegex(workdir.PullErr, "changed since analysis"):
            self.approve(seeded.workdir_id, report["id"])

    @unittest.skipUnless(GIT, "needs git")
    def test_from_a_commit(self) -> None:
        """By default the committed files are pulled, and the VCS reports on the copy."""
        def git(*args: str) -> None:
            env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
            subprocess.run([str(GIT), "-C", str(self.project), *args], check=True, capture_output=True, env=env)  # nosec: B603  # git, as a list.

        git("init", "-q", "-b", "main")
        git("-c", "user.email=t@example.com", "-c", "user.name=T", "add", "-A")
        git("-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q", "-m", "first")
        (self.files / "env" / "Dockerfile").write_text("FROM uncommitted\n", encoding="utf-8")
        suc = self.config().raw()
        suc["found-project"] = {
            "project-dir": str(self.project), "vcs-root": str(self.project), "vcs": "git", "project-branch": "main",
        }
        config = UserConfig(suc)
        seeded = seed.seed(config, generate=generator.run, path_env=self.path_env)
        self.assertEqual((seeded.path / "files" / "env" / "Dockerfile").read_text(encoding="utf-8"), "FROM scratch\n")
        provenance = workdir.open_workdir(config, seeded.workdir_id).provenance
        self.assertEqual(provenance.source["from"], "commit")
        self.assertEqual({v["status"] for v in provenance.vcs["files"].values()}, {"committed"})
        seeded = seed.seed(config, generate=generator.run, path_env=self.path_env, working_tree=True)
        self.assertEqual((seeded.path / "files" / "env" / "Dockerfile").read_text(encoding="utf-8"), "FROM uncommitted\n")
        provenance = workdir.open_workdir(config, seeded.workdir_id).provenance
        self.assertEqual(provenance.vcs["files"]["env/Dockerfile"]["status"], "modified")
        report = analyze.analyze(config, seeded.workdir_id, generate=generator.run, path_env=self.path_env)
        dockerfile = next(f for f in report["files"] if f["path"]["text"] == "env/Dockerfile")
        self.assertTrue(any("nobody committed" in a for a in dockerfile["annotations"]))

    def test_main(self) -> None:
        """The plumbing commands print JSON."""
        out = io.StringIO()
        suc = self.tmp / "suc.json"
        suc.write_text(json.dumps(self.config().raw()), encoding="utf-8")
        with contextlib.redirect_stdout(out):
            code = main_pull.main(["main_pull.py", str(suc), "pull", "seed", "--working-tree"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.getvalue())["id"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main_pull.main(["main_pull.py", str(suc), "pull", "analyze", "../escape"])
        self.assertEqual(code, 1)
        self.assertIn("isn't a working directory id", err.getvalue())

    def test_subcommand(self) -> None:
        """One sub-command runs another with the SUC it was given."""
        seeded = self.seed()
        finished = subcommand.run(self.config(), "pull", ["analyze", seeded.workdir_id], capture=True)
        self.assertEqual(finished.code, 0)
        assert finished.stdout is not None  # nosec: B101  # captured
        self.assertEqual(json.loads(finished.stdout)["workdir"], seeded.workdir_id)
