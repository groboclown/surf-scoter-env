#SPDX:MIT
"""Test the main_cleanup script, and the cleanup library it uses."""
import contextlib
import datetime
import io
import os
import shutil
import subprocess  # nosec: B404  # A process to outlive, for a stale record.
import sys
import unittest
from pathlib import Path

# The scripts import the library as their own directory's, as they do when
# they run.
SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
import main_cleanup
from surf_scoter_lib import cleanup, ssh_hosts
from surf_scoter_lib.active_env import ActiveEnvironmentSet, EnvironmentDesc
from surf_scoter_lib.user_config import UserConfig
from surf_scoter_lib.verified import records, store

from .test_main_revoke import POSIX, StoreTest

PREFIX = "local-surf-scoter/"


def dead_pid() -> int:
    """The number of a process that has ended."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])  # nosec: B603  # This interpreter, as a list.
    child.wait()
    return child.pid


class ProcessAliveTest(unittest.TestCase):
    """Test telling whether a run's process is still there."""

    @unittest.skipUnless(POSIX, "process checks")
    def test_alive_and_gone(self) -> None:
        """This process is; one that ended isn't; no process can't be told."""
        self.assertIs(cleanup.process_alive(os.getpid()), True)
        self.assertIs(cleanup.process_alive(dead_pid()), False)
        self.assertIsNone(cleanup.process_alive(0))


@unittest.skipUnless(POSIX, "owner and mode checks, and process checks")
class CleanupTest(StoreTest):
    """Finding and removing what's left behind."""

    def setUp(self) -> None:
        super().setUp()
        self.state = self.tmp / "state"
        self.state.mkdir(mode=0o700)
        self.docker_log = self.tmp / "docker.log"
        self.images = ["local-surf-scoter/scoter-me-2000-a:latest", f"{PREFIX}firewall-router:latest",
                       f"{PREFIX}scoter-me-2000-z:latest", "<none>:<none>"]
        self.docker = self.tmp / "docker"
        self.docker.write_text(
            "#!/bin/sh\n"
            f"echo \"$@\" >> '{self.docker_log}'\n"
            f"[ \"$1\" = list ] && cat '{self.tmp / 'images'}'\n"
            "exit 0\n",
            encoding="utf-8",
        )
        os.chmod(self.docker, 0o700)
        (self.tmp / "images").write_text("\n".join(self.images) + "\n", encoding="utf-8")
        self.ssh = self.tmp / "ssh"
        self.ssh.mkdir(mode=0o700)
        (self.ssh / "key").write_text("", encoding="utf-8")
        (self.ssh / "config").write_text("", encoding="utf-8")
        self.ssh_mode = "ask"

    def config(
        self, project: Path | None = None, *, outside: bool = False, branch: str | None = None,
    ) -> UserConfig:
        """The store test's, with the run's state and images."""
        suc = super().config(project, outside=outside, branch=branch).raw()
        assert isinstance(suc, dict)  # nosec: B101  # narrowing for the type checker
        resolved = suc["resolved"]
        assert isinstance(resolved, dict)  # nosec: B101  # narrowing for the type checker
        resolved["scoter"]["paths"] = {  # type: ignore[index]
            "state-dir": str(self.state),
            "running-envs": str(self.state / "running-env.json"),
            "running-envs-lock": str(self.state / "running-env.lock"),
        }
        resolved["scoter"]["values"]["backup-file-extension"] = ".backup"  # type: ignore[index]
        resolved["ssh"] = {
            "paths": {"key-file": str(self.ssh / "key"), "config-file": str(self.ssh / "config")},
            "values": {"host-prefix": "scoter-", "config-cleanup": self.ssh_mode},
            "runs": {
                "remove-known-host": [str(self.docker), "forget", "{x:host}"],
                "proxy-command": [str(self.docker), "exec", "%h", "{x:port}"],
            },
        }
        resolved["dev-env"] = {"values": {
            "dev-env-image-prefix": PREFIX,
            "firewall-router-image": f"{PREFIX}firewall-router",
            "network-gateway-image": f"{PREFIX}network-gateway",
        }}
        exe = str(self.docker)
        resolved["docker"] = {"runs": {
            "list-images": [exe, "list", "{x:pattern}"],
            "remove-image": [exe, "rm", "{x:name}"],
            "prune-images": [exe, "prune"],
        }}
        return UserConfig(suc)

    def record_run(self, suffix: str, pid: int, **fields: str) -> EnvironmentDesc:
        """Record a run as `scoter-me-2000-SUFFIX`: a reservation, unless it's given its files."""
        values = {
            "dev_env_name": "", "firewall_router_name": "", "network_gateway_name": "",
            "project_dir": "", "resources_dir": "", "workspace_dir": "", "setup_dir": "",
            "compose_file": "", "compose_env_file": "", **fields,
        }
        env = EnvironmentDesc(
            host=f"scoter-me-2000-{suffix}",
            dev_env_ssh_port="2000", dev_env_ssh_user="me",
            created_at="2026-10-21T10:00:00", suc={}, pid=pid, **values,
        )
        with ActiveEnvironmentSet.locked(self.state / "running-env.json", self.state / "running-env.lock") as envs:
            envs.environs[env.host] = env
        return env

    def hosts(self) -> list[str]:
        envs = ActiveEnvironmentSet.read(self.state / "running-env.json")
        return sorted(env.host for env in envs.environs.values())

    def run_cleanup(self, **options: object) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            # Outside any project, as `scoter cleanup` runs.
            code = main_cleanup.run_cleanup(self.config(outside=True), **options)  # type: ignore[arg-type]
        return code, out.getvalue()

    def test_nothing(self) -> None:
        """Nothing left behind: said so."""
        self.assertEqual(self.run_cleanup(confirm=main_cleanup.Confirm.FORCE), (0, "Nothing to clean up.\n"))

    def test_stale_runs_and_state(self) -> None:
        """A record whose run is gone, and state no record uses, go; a running one's stay."""
        self.record_run("a", os.getpid())
        self.record_run("b", dead_pid())
        (self.state / "environments" / "scoter-me-2000-a").mkdir(parents=True)
        (self.state / "environments" / "scoter-me-2000-q").mkdir()
        old = self.state / "exempt-old"
        old.mkdir()
        hours_ago = (datetime.datetime.now() - datetime.timedelta(hours=2)).timestamp()
        os.utime(old, (hours_ago, hours_ago))
        (self.state / "exempt-new").mkdir()
        code, out = self.run_cleanup(confirm=main_cleanup.Confirm.DRY_RUN)
        self.assertEqual(code, 0)
        self.assertIn("scoter-me-2000-b  reserved", out)
        self.assertIn(str(self.state / "environments" / "scoter-me-2000-q"), out)
        self.assertIn(str(old), out)
        self.assertNotIn("exempt-new", out)
        self.assertIn("Nothing was removed (--dry-run).", out)
        self.assertEqual(self.hosts(), ["scoter-me-2000-a", "scoter-me-2000-b"])
        code, out = self.run_cleanup(confirm=main_cleanup.Confirm.FORCE)
        self.assertEqual(code, 0, out)
        self.assertIn("Stopped scoter-me-2000-b.", out)
        self.assertEqual(self.hosts(), ["scoter-me-2000-a"])
        self.assertEqual(os.listdir(self.state / "environments"), ["scoter-me-2000-a"])
        self.assertFalse(old.exists())
        self.assertTrue((self.state / "exempt-new").exists())

    def test_gone_project(self) -> None:
        """A project whose directory is gone: its acceptances are revoked; another's stay."""
        kept = self.accept("FROM scratch\n", "main")
        other = self.new_project("other")
        self.accept("FROM alpine\n", None, other)
        shutil.rmtree(other)
        code, out = self.run_cleanup(confirm=main_cleanup.Confirm.FORCE)
        self.assertEqual(code, 0, out)
        self.assertIn(f"{other}  (1 acceptance)", out)
        self.assertIn(f"Revoked 1 acceptance of {other}.", out)
        verified_store = store.VerifiedStore.open(self.verified)
        self.assertEqual(verified_store.index(), {str(self.project): store.environment_id(self.project)})
        self.assertEqual(self.remaining(), [kept.id])

    def test_store_leftovers(self) -> None:
        """What no record names goes: directories, blobs, temporary files; the rest stays."""
        kept = self.accept("FROM scratch\n", "main")
        verified_store = store.VerifiedStore.open(self.verified)
        env_id = store.environment_id(self.project)
        with verified_store.locked():
            verified_store.write_bytes(env_id, records.ACCEPTANCES_DIR, "orphan", "accepted.json", data=b"{}")
            verified_store.write_bytes("gone-0123456789ab", "environment.json", data=b"{}")
            verified_store.put_blob(b"nobody uses this")
        (self.verified / ".tmp-left").write_bytes(b"x")
        found = cleanup.find_store_leftovers(verified_store)
        self.assertEqual(found.environments, ["gone-0123456789ab"])
        self.assertEqual(found.acceptances, [(env_id, "orphan")])
        self.assertEqual((found.unused_blobs, found.temp_files), (1, 1))
        code, out = self.run_cleanup(ask=lambda _: "y")
        self.assertEqual(code, 0, out)
        self.assertIn("1 copied file no acceptance uses", out)
        self.assertFalse(cleanup.find_store_leftovers(verified_store))
        self.assertEqual(self.remaining(self.project), [kept.id])
        self.assertTrue(self.acceptance_dir(kept).is_dir())

    def test_asked(self) -> None:
        """Anything but yes, and without a terminal, nothing goes."""
        self.record_run("b", dead_pid())
        self.assertIn("Nothing was removed.", self.run_cleanup(ask=lambda _: "n")[1])
        stdin = sys.stdin
        sys.stdin = io.StringIO()
        try:
            out = self.run_cleanup()[1]
        finally:
            sys.stdin = stdin
        self.assertIn("give --force", out)
        self.assertEqual(self.hosts(), ["scoter-me-2000-b"])

    def test_workspaces(self) -> None:
        """Only with `Extras.WORKSPACES`: those no running environment uses, or is starting with."""
        root = self.state / "workspaces"
        for name in ("unused", "used", store.environment_id(self.project)):
            (root / name / "workspace").mkdir(parents=True)
        self.record_run("a", os.getpid(), workspace_dir=str(root / "used" / "workspace"))
        self.record_run("c", os.getpid(), project_dir=str(self.project))
        self.assertEqual(self.run_cleanup(confirm=main_cleanup.Confirm.FORCE)[1], "Nothing to clean up.\n")
        code, out = self.run_cleanup(confirm=main_cleanup.Confirm.FORCE, extras=main_cleanup.Extras.WORKSPACES)
        self.assertEqual(code, 0, out)
        self.assertIn(f"Removed {root / 'unused'}.", out)
        self.assertEqual(sorted(os.listdir(root)), sorted(["used", store.environment_id(self.project)]))

    def test_images(self) -> None:
        """Only with `Extras.IMAGES`: those no running environment uses, then the dangling layers."""
        self.record_run("a", os.getpid())
        code, out = self.run_cleanup(confirm=main_cleanup.Confirm.FORCE, extras=main_cleanup.Extras.IMAGES)
        self.assertEqual(code, 0, out)
        self.assertIn(f"Removed {PREFIX}scoter-me-2000-z:latest.", out)
        log = self.docker_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(log, [
            f"list {PREFIX}*", f"rm {PREFIX}scoter-me-2000-z:latest", "prune",
        ])

    def ssh_config(self) -> str:
        return (self.ssh / "config").read_text(encoding="utf-8")

    def write_ssh_hosts(self) -> None:
        """A block of the user's, and the tools' blocks for a running and a stopped environment."""
        (self.ssh / "config").write_text("Host scoter-mine\n  User me\n\n", encoding="utf-8")
        for host in ("scoter-me-2000-a", "scoter-me-2000-z"):
            ssh_hosts.write_host(self.config(), host, "me", "2000")
        self.record_run("a", os.getpid())

    def test_ssh_ask(self) -> None:
        """`ask`: the stopped environment's entry is asked about on its own, even with `Confirm.FORCE`."""
        self.write_ssh_hosts()
        code, out = self.run_cleanup(confirm=main_cleanup.Confirm.FORCE, ask=lambda prompt: "n")
        self.assertEqual(code, 0, out)
        self.assertIn("SSH config entries for environments that aren't running:\n  scoter-me-2000-z\n", out)
        self.assertIn("The ssh config entries were kept.", out)
        self.assertIn("Host scoter-me-2000-z", self.ssh_config())
        asked: list[str] = []
        code, out = self.run_cleanup(confirm=main_cleanup.Confirm.FORCE, ask=lambda prompt: asked.append(prompt) or "y")
        self.assertEqual(asked, [main_cleanup.SSH_PROMPT])
        self.assertIn("Removed the ssh entries for scoter-me-2000-z.", out)
        config = self.ssh_config()
        self.assertNotIn("Host scoter-me-2000-z", config)
        self.assertIn("Host scoter-me-2000-a", config)
        self.assertIn("Host scoter-mine\n  User me\n", config)
        self.assertIn("forget scoter-me-2000-z", self.docker_log.read_text(encoding="utf-8"))

    def test_ssh_auto_and_no(self) -> None:
        """`auto`: removed with the rest; `no`: only counted."""
        self.write_ssh_hosts()
        self.ssh_mode = "no"
        code, out = self.run_cleanup(confirm=main_cleanup.Confirm.FORCE)
        self.assertIn("Your ssh config has 1 entry for environments that aren't running", out)
        self.assertIn("Host scoter-me-2000-z", self.ssh_config())
        self.ssh_mode = "auto"
        code, out = self.run_cleanup(confirm=main_cleanup.Confirm.FORCE)
        self.assertEqual(code, 0, out)
        self.assertIn("Removed the ssh entries for scoter-me-2000-z.", out)
        self.assertNotIn("Host scoter-me-2000-z", self.ssh_config())
