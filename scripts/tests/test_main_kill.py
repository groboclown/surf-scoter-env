#SPDX:MIT
"""Test the main_kill script."""
import contextlib
import io
import json
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
import main_kill
from surf_scoter_lib.active_env import ActiveEnvironmentSet, EnvironmentDesc
from surf_scoter_lib.user_config import UserConfig

# Records its arguments in the file named by RECORD, and exits with EXIT (0 by default).
RECORDER = (
    "import json, os, sys; "
    "open(os.environ['RECORD'], 'a').write(json.dumps(sys.argv[1:]) + '\\n'); "
    "sys.exit(int(os.environ.get('EXIT', '0')))"
)


class KillTest(unittest.TestCase):
    """Stopping an environment, from its record."""

    def setUp(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.tmp = Path(os.path.realpath(directory))
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.project = self.tmp / "proj"
        self.clone = self.tmp / "projects" / "proj-0123456789ab" / "project-clone"
        self.clone.mkdir(parents=True)
        self.project.mkdir()
        self.resources = self.tmp / "resources"
        self.resources.mkdir()
        for name in ("docker-compose.yaml", "compose.env"):
            (self.resources / name).write_text("", encoding="utf-8")
        self.record = self.tmp / "record"
        os.environ["RECORD"] = str(self.record)
        self.addCleanup(os.environ.pop, "RECORD", None)
        self.addCleanup(os.environ.pop, "EXIT", None)

    def suc(self, *, project: bool = True) -> dict:
        recorder = [sys.executable, "-c", RECORDER]
        return {
            "active-config-locations": {"project-dir": str(self.project) if project else None},
            "current-project-setup": {"vcs": "git"} if project else None,
            "resolved": {
                "scoter": {"paths": {
                    "state-dir": str(self.state),
                    "running-envs": str(self.state / "running-env.json"),
                    "running-envs-lock": str(self.state / "running-env.json.lock"),
                }},
                "docker": {"runs": {
                    "compose-down": [*recorder, "down", "{x:compose-file}"],
                    "remove-volume": [*recorder, "volume-rm", "{x:name}"],
                }},
                "git-vcs": {"runs": {"fetch-changes-into": [*recorder, "fetch", "{x:project-clone-dir}", "{x:branch}"]}},
                "project-state": {"values": {"persistent-store-volume": "scoter-proj-0123456789ab-store"}},
            },
        }

    def start(self, suffix: str = "a", *, reserved: bool = False) -> EnvironmentDesc:
        """Record an environment, as `scoter run` does (or only its reservation)."""
        env = EnvironmentDesc(
            host=f"dev-me-2222-{suffix}",
            dev_env_name="dev", firewall_router_name="fr", network_gateway_name="ng",
            project_dir=str(self.project), resources_dir="" if reserved else str(self.resources),
            project_clone_dir="" if reserved else str(self.clone), setup_dir="",
            compose_file="" if reserved else str(self.resources / "docker-compose.yaml"),
            compose_env_file="" if reserved else str(self.resources / "compose.env"),
            dev_env_ssh_port="2222", dev_env_ssh_user="me", created_at="2026-10-21T00:00:00",
            suc={} if reserved else self.suc(), pid=1,
        )
        with ActiveEnvironmentSet.locked(self.state / "running-env.json", self.state / "running-env.json.lock") as envs:
            envs.environs[env.host] = env
        (self.state / "environments" / env.host).mkdir(parents=True)
        return env

    def kill(self, *, host: str | None = None, fetch: str | None = None,
             clean: main_kill.Clean = main_kill.Clean.NOTHING,
             project: bool = True) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main_kill.run_kill(
                UserConfig(self.suc(project=project)), host=host, fetch_branch=fetch, clean=clean,
            )
        return code, out.getvalue() + err.getvalue()

    def ran(self) -> list[list[str]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def listed(self) -> list[str]:
        return [e.host for e in ActiveEnvironmentSet.read(self.state / "running-env.json").environs.values()]

    def test_projects_environment(self) -> None:
        """Without --host, the project's one: its containers stopped, it's forgotten, its clone and store kept."""
        env = self.start()
        code, text = self.kill()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.ran(), [["down", env.compose_file]])
        self.assertEqual(self.listed(), [])
        self.assertFalse((self.state / "environments" / env.host).exists())
        self.assertTrue(self.clone.is_dir())
        self.assertIn("The project clone is kept", text)

    def test_by_host_outside_project(self) -> None:
        """--host stops it without a project."""
        env = self.start()
        code, _ = self.kill(host=env.host, project=False)
        self.assertEqual(code, 0)
        self.assertEqual(self.listed(), [])

    def test_choosing(self) -> None:
        """An unknown host, no project, or two of the project's: nothing is stopped."""
        self.start("a")
        self.start("b")
        self.assertEqual(self.kill(host="nope")[0], 1)
        code, text = self.kill()
        self.assertEqual(code, 1)
        self.assertIn("More than one environment", text)
        code, text = self.kill(project=False)
        self.assertEqual(code, 1)
        self.assertIn("give the environment with --host", text)
        self.assertEqual(self.ran(), [])
        self.assertEqual(len(self.listed()), 2)

    def test_reservation(self) -> None:
        """A run that crashed before it started anything: only forgotten."""
        self.start(reserved=True)
        code, text = self.kill()
        self.assertEqual(code, 0, text)
        self.assertIn("only reserved", text)
        self.assertEqual(self.ran(), [])
        self.assertEqual(self.listed(), [])

    def test_compose_down_fails(self) -> None:
        """Containers that won't stop: still listed, to try again."""
        self.start()
        os.environ["EXIT"] = "1"
        code, text = self.kill()
        self.assertEqual(code, 1)
        self.assertIn("still listed", text)
        self.assertEqual(len(self.listed()), 1)

    def test_fetch_and_clean(self) -> None:
        """--fetch brings the clone's branch into the project first; --clean-project-clone removes the clone only."""
        env = self.start()
        code, text = self.kill(fetch="feature", clean=main_kill.Clean.PROJECT_CLONE)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.ran(), [["down", env.compose_file], ["fetch", str(self.clone), "feature"]])
        self.assertFalse(self.clone.exists())

    def test_clean_persistent_store(self) -> None:
        """--clean-persistent-store removes the volume, and keeps the clone."""
        env = self.start()
        code, text = self.kill(clean=main_kill.Clean.PERSISTENT_STORE)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.ran(), [["down", env.compose_file], ["volume-rm", "scoter-proj-0123456789ab-store"]])
        self.assertTrue(self.clone.is_dir())

    def test_clean_both(self) -> None:
        """Both clean options together."""
        self.start()
        code, text = self.kill(clean=main_kill.Clean.PROJECT_CLONE | main_kill.Clean.PERSISTENT_STORE)
        self.assertEqual(code, 0, text)
        self.assertFalse(self.clone.exists())
        self.assertIn(["volume-rm", "scoter-proj-0123456789ab-store"], self.ran())

    def test_clean_aliases(self) -> None:
        """`--clean-project` is `--clean-project-clone`."""
        parsed = {}

        def run_kill(config: UserConfig, **kwargs: object) -> int:
            parsed.update(kwargs)
            return 0
        original = main_kill.run_kill
        main_kill.run_kill = run_kill  # type: ignore[assignment]
        self.addCleanup(setattr, main_kill, "run_kill", original)
        universal = self.tmp / "suc.json"
        universal.write_text(json.dumps(self.suc()), encoding="utf-8")
        for flags, clean in [
            (["--clean-project"], main_kill.Clean.PROJECT_CLONE),
            (["--clean-project-clone"], main_kill.Clean.PROJECT_CLONE),
            (["--clean-persistent-store"], main_kill.Clean.PERSISTENT_STORE),
            ([], main_kill.Clean.NOTHING),
        ]:
            main_kill.main(["main_kill.py", str(universal), "kill", *flags])
            self.assertEqual(parsed["clean"], clean, flags)

    def test_clean_keeps_the_project(self) -> None:
        """A clone that is, or holds, the project isn't removed."""
        env = self.start()
        with ActiveEnvironmentSet.locked(self.state / "running-env.json", self.state / "running-env.json.lock") as envs:
            envs.environs[env.host] = EnvironmentDesc(**{**env.__dict__, "project_clone_dir": str(self.tmp)})
        code, text = self.kill(clean=main_kill.Clean.PROJECT_CLONE)
        self.assertEqual(code, 0, text)
        self.assertIn("is, or holds, the project directory", text)
        self.assertTrue(self.project.is_dir())


if __name__ == "__main__":
    unittest.main()
