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
        self.workspace = self.project / ".dev-env" / "workspace"
        self.workspace.mkdir(parents=True)
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
                "docker": {"runs": {"compose-down": [*recorder, "down", "{x:compose-file}"]}},
                "git-vcs": {"runs": {"fetch-changes-into": [*recorder, "fetch", "{x:workspace-dir}", "{x:branch}"]}},
            },
        }

    def start(self, suffix: str = "a", *, reserved: bool = False) -> EnvironmentDesc:
        """Record an environment, as `scoter run` does (or only its reservation)."""
        env = EnvironmentDesc(
            host_prefix="dev-me-2222-", host_suffix=suffix,
            dev_env_name="dev", firewall_router_name="fr", network_gateway_name="ng",
            project_dir=str(self.project), resources_dir="" if reserved else str(self.resources),
            workspace_dir="" if reserved else str(self.workspace), setup_dir="",
            compose_file="" if reserved else str(self.resources / "docker-compose.yaml"),
            compose_env_file="" if reserved else str(self.resources / "compose.env"),
            dev_env_ssh_port="2222", dev_env_ssh_user="me", created_at="2026-10-21T00:00:00",
            suc={} if reserved else self.suc(), pid=1,
        )
        with ActiveEnvironmentSet.locked(self.state / "running-env.json", self.state / "running-env.json.lock") as envs:
            envs.environs[suffix] = env
        (self.state / "environments" / env.host).mkdir(parents=True)
        return env

    def kill(self, *, host: str | None = None, fetch: str | None = None, clean: bool = False,
             project: bool = True) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main_kill.run_kill(
                UserConfig(self.suc(project=project)), host=host, fetch_branch=fetch, force_clean=clean,
            )
        return code, out.getvalue() + err.getvalue()

    def ran(self) -> list[list[str]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def listed(self) -> list[str]:
        return [e.host for e in ActiveEnvironmentSet.read(self.state / "running-env.json").environs.values()]

    def test_the_projects_environment(self) -> None:
        """Without --host, the project's one: its containers stopped, it's forgotten, the workspace kept."""
        env = self.start()
        code, text = self.kill()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.ran(), [["down", env.compose_file]])
        self.assertEqual(self.listed(), [])
        self.assertFalse((self.state / "environments" / env.host).exists())
        self.assertTrue(self.workspace.is_dir())
        self.assertIn("The workspace is kept", text)

    def test_by_host_outside_a_project(self) -> None:
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
        """--fetch brings the workspace's branch into the project first; --force-clean removes the workspace."""
        env = self.start()
        code, text = self.kill(fetch="feature", clean=True)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.ran(), [["down", env.compose_file], ["fetch", str(self.workspace), "feature"]])
        self.assertFalse(self.workspace.exists())

    def test_clean_never_removes_the_project(self) -> None:
        """A workspace that is, or holds, the project isn't removed."""
        env = self.start()
        with ActiveEnvironmentSet.locked(self.state / "running-env.json", self.state / "running-env.json.lock") as envs:
            envs.environs["a"] = EnvironmentDesc(**{**env.__dict__, "workspace_dir": str(self.tmp)})
        code, text = self.kill(clean=True)
        self.assertEqual(code, 0, text)
        self.assertIn("is, or holds, the project directory", text)
        self.assertTrue(self.project.is_dir())


if __name__ == "__main__":
    unittest.main()
