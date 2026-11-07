#SPDX:MIT
"""Test what `scoter run` writes for compose, and how it makes the project's clone."""
import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

# The scripts import the library as their own directory's, as they do when
# they run.
SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
import main_run
from surf_scoter_lib import persistent_store
from surf_scoter_lib.err import ConfigErr
from surf_scoter_lib.user_config import UserConfig

MOUNTS = {"project-clone": "/project", "persistent-store": "/persistent-store", "setup": "/setup"}


def suc(
    *, mounts: dict[str, str] | None = None, project_clone: str | None = None,
    clone_dir: str = "/state/projects/p/project-clone", volume: str = "scoter-p-store",
    vcs: dict[str, object] | None = None, locations: dict[str, str] | None = None,
) -> UserConfig:
    """Enough of a universal configuration file for the `.env` file and the clone."""
    container: dict[str, object] = {"dev-container-path": "./env"}
    if project_clone is not None:
        container["mounts"] = {"project-clone": project_clone}
    return UserConfig({
        "active-config-locations": locations or {},
        "current-project-setup": {"container": container},
        "dev-container": {"mounts": mounts or MOUNTS, "sshd-port": 22022, "sshd-port-env": "SCOTER_SSHD_PORT"},
        "resolved": {
            "dev-env": {"values": {
                "firewall-router-image": "local-surf-scoter/firewall-router:latest",
                "network-gateway-image": "local-surf-scoter/network-gateway:latest",
                "dev-env-image-prefix": "local-surf-scoter/",
                "network-gateway-config-mount": "/user-space",
                "pids-limit": "128", "mem-limit": "4g", "cpus": "2", "logging-max-size": "10m",
            }},
            "project-state": {
                "paths": {"project-clone-dir": clone_dir},
                "values": {"persistent-store-volume": volume},
            },
            **({"test-vcs": vcs} if vcs else {}),
        },
    })


# A stand-in VCS: records its arguments and working directory in RECORD, and
# does what the real one would to the files: `clone DIR` makes DIR/.git/config,
# `bundle FILE` writes FILE (unless EMPTY is set, as for an empty repository).
VCS = """
import json, os, sys
args = sys.argv[1:]
open(os.environ["RECORD"], "a").write(json.dumps([os.getcwd(), *args]) + "\\n")
if args[0] == "clone":
    os.makedirs(os.path.join(args[1], ".git"))
    open(os.path.join(args[1], ".git", "config"), "w").write("[core]\\n")
elif args[0] == "bundle":
    if os.environ.get("EMPTY"):
        sys.exit(128)
    open(args[1], "w").write("bundle")
"""


class EnvFileTest(unittest.TestCase):
    """The compose `.env` file."""

    def setUp(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.tmp = Path(directory)

    def write(self, config: UserConfig, settings: str = "/state/setup") -> dict[str, str]:
        file = self.tmp / "compose.env"
        main_run._write_compose_env_file(
            config=config, file=file, host="scoter-me-p",
            clone_dir=Path("/state/projects/p/project-clone"), volume="scoter-p-store",
            settings_dir=Path(settings), gateway_config=Path("/state/resources/gateway-config"),
        )
        values: dict[str, str] = {}
        for line in file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            self.assertTrue(value.startswith("'") and value.endswith("'"), line)
            values[key] = value[1:-1]
        return values

    def test_values(self) -> None:
        """Every value single-quoted; the mounts and the volume as the run chose them."""
        values = self.write(suc())
        self.assertEqual(values["PROJECT_CLONE_HOST"], "/state/projects/p/project-clone")
        self.assertEqual(values["PROJECT_CLONE_CONTAINER"], "/project")
        self.assertEqual(values["PERSISTENT_STORE_CONTAINER"], "/persistent-store")
        self.assertEqual(values["PERSISTENT_STORE_VOLUME"], "scoter-p-store")
        self.assertEqual(values["SETTINGS_CONTAINER"], "/setup")
        self.assertEqual(values["FIREWALL_ROUTER"], "local-surf-scoter/firewall-router:latest")
        self.assertNotIn("WORKSPACE_HOST", values)

    def test_project_clone_override(self) -> None:
        """The project file's `container: mounts: project-clone` takes the container's place."""
        self.assertEqual(self.write(suc(project_clone="/src/p"))["PROJECT_CLONE_CONTAINER"], "/src/p")

    def test_mount_points_refused(self) -> None:
        """A mount point that isn't a plain container path stops the run (DEV-2)."""
        for bad in ["/w\nWORKSPACE_HOST=/home/you", "/w:/x", "/w,ro", "rel", "/a/../b", "/w x", "/"]:
            with self.subTest(bad=bad), self.assertRaises(ConfigErr):
                self.write(suc(mounts={**MOUNTS, "persistent-store": bad}))
        with self.assertRaises(ConfigErr):
            self.write(suc(project_clone="/x\ny"))
        with self.assertRaises(ConfigErr):
            self.write(suc(mounts={"project-clone": "/p", "setup": "/s"}))

    def test_host_paths(self) -> None:
        """A setup's host path may have spaces or `$`; never a newline or a quote."""
        self.assertEqual(self.write(suc(), settings="/Users/A B/$x")["SETTINGS_HOST"], "/Users/A B/$x")
        for bad in ["/a\nB=c", "/a'b", "/a\\'b", "/a\\\\b", "/a\rb"]:
            with self.subTest(bad=bad), self.assertRaises(ConfigErr):
                self.write(suc(), settings=bad)

    def test_env_line(self) -> None:
        self.assertEqual(main_run.env_line("A", "x y$z#"), "A='x y$z#'\n")
        self.assertEqual(main_run.env_line("A", "C:\\Users\\x"), "A='C:\\Users\\x'\n")
        with self.assertRaises(ConfigErr):
            main_run.env_line("A", "x\x00")


class VolumeNameTest(unittest.TestCase):
    """The persistent store's volume name."""

    def test_names(self) -> None:
        self.assertEqual(persistent_store.volume_name(suc(volume="scoter-p-0123-store")), "scoter-p-0123-store")
        for bad in ["", "Upper", "-x", "a b", "a/b", "a:b", "a\nb", "x" * 129]:
            with self.subTest(bad=bad), self.assertRaises(ConfigErr):
                persistent_store.volume_name(suc(volume=bad))


class CreateVolumeTest(unittest.TestCase):
    """Creating the persistent store only when it isn't there (Podman refuses a second create)."""

    def test_create(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        record = Path(directory) / "record"
        exists = Path(directory) / "exists"
        tool = [sys.executable, "-c", (
            "import os, sys; open(sys.argv[2], 'a').write(sys.argv[1] + '\\n'); "
            "sys.exit(0 if sys.argv[1] != 'inspect' or os.path.exists(sys.argv[3]) else 1)"
        )]
        config = UserConfig({"resolved": {"docker": {"runs": {
            "inspect-volume": [*tool, "inspect", str(record), str(exists)],
            "create-volume": [*tool, "create", str(record), str(exists)],
        }}}})
        with contextlib.redirect_stdout(io.StringIO()):
            persistent_store.create(config, "v")
            self.assertEqual(record.read_text(encoding="utf-8").split(), ["inspect", "create"])
            record.unlink()
            exists.touch()
            persistent_store.create(config, "v")
        self.assertEqual(record.read_text(encoding="utf-8").split(), ["inspect"])


class ProjectCloneTest(unittest.TestCase):
    """Making the project's clone."""

    def setUp(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.tmp = Path(os.path.realpath(directory))
        self.clone = self.tmp / "state" / "project-clone"
        self.clone.parent.mkdir()
        self.setup = self.tmp / "state" / "setup"
        self.setup.mkdir()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.record = self.tmp / "record"
        os.environ["RECORD"] = str(self.record)
        self.addCleanup(os.environ.pop, "RECORD", None)
        self.addCleanup(os.environ.pop, "EMPTY", None)

    def make(self, clean: bool = False, *, vcs: bool = False, fetch: bool = True) -> Path:
        exe = [sys.executable, "-c", VCS]
        runs = {
            "local-clone": [*exe, "clone", "{x:project-clone-dir}"],
            "refresh-bundle": [*exe, "bundle", "{x:bundle}"],
            "set-clone-origin": [*exe, "config", "{x:project-clone-dir}/.git/config", "{x:origin}"],
        }
        config = suc(
            clone_dir=str(self.clone), vcs={"runs": runs} if vcs else None,
            locations={"vcs-root": str(self.repo), "project-dir": str(self.repo)},
        )
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main_run._setup_project_clone(
                config, "test-vcs" if vcs else None, clean, fetch, self.setup,
            )

    def ran(self) -> list[list[str]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]

    def test_the_clone_is_the_directory(self) -> None:
        """Without a VCS, an empty directory, at the setup's path itself: no sub-path."""
        self.assertEqual(self.make(), self.clone)
        self.assertTrue(self.clone.is_dir())
        (self.clone / "work").write_text("x", encoding="utf-8")
        self.make()
        self.assertTrue((self.clone / "work").exists())
        self.make(clean=True)
        self.assertEqual(os.listdir(self.clone), [])

    def test_symlink_refused(self) -> None:
        """A symlink where the clone goes is refused, not followed or removed (FILE-10)."""
        target = self.tmp / "elsewhere"
        target.mkdir()
        (target / "keep").write_text("x", encoding="utf-8")
        self.clone.symlink_to(target)
        for clean in (False, True):
            with self.subTest(clean=clean), self.assertRaises(ConfigErr):
                self.make(clean=clean)
        self.assertTrue((target / "keep").exists())

    def test_never_runs_in_the_clone(self) -> None:
        """The bundle is made in your repository; nothing runs with the clone as its directory (FILE-1)."""
        self.make(vcs=True)
        bundle = self.setup / "project.bundle"
        self.assertEqual(self.ran(), [
            [str(self.repo), "bundle", str(self.setup / "project.bundle.tmp")],
            [str(self.repo), "clone", str(self.clone)],
            [str(self.setup), "config", f"{self.clone}/.git/config", "/setup/project.bundle"],
        ])
        self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), 0o644)
        self.assertFalse((self.setup / "project.bundle.tmp").exists())
        # The next run only refreshes the bundle: the clone is the container's now.
        self.record.unlink()
        self.make(vcs=True)
        self.assertEqual(self.ran(), [[str(self.repo), "bundle", str(self.setup / "project.bundle.tmp")]])
        for args in self.ran():
            self.assertFalse(Path(args[0]).is_relative_to(self.clone), args)

    def test_no_project_fetch(self) -> None:
        """--no-project-fetch leaves an existing clone's bundle alone, but a new clone gets one."""
        self.make(vcs=True)
        self.record.unlink()
        self.make(vcs=True, fetch=False)
        self.assertEqual(self.ran(), [])

    def test_empty_repository(self) -> None:
        """No bundle (an empty repository): the clone is still made, and nothing is left behind."""
        os.environ["EMPTY"] = "1"
        self.make(vcs=True)
        self.assertTrue(self.clone.is_dir())
        self.assertEqual(os.listdir(self.setup), [])

    def test_file_refused(self) -> None:
        self.clone.write_text("x", encoding="utf-8")
        with self.assertRaises(ConfigErr):
            self.make()


if __name__ == "__main__":
    unittest.main()
