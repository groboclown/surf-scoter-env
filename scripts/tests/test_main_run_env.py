#SPDX:MIT
"""Test what `scoter run` writes for compose, and how it makes the project's clone."""
import contextlib
import io
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
import main_run
from surf_scoter_lib import persistent_store
from surf_scoter_lib.err import ConfigErr
from surf_scoter_lib.user_config import UserConfig

MOUNTS = {"project-clone": "/project", "persistent-store": "/persistent-store", "setup": "/setup"}


def suc(
    *, mounts: dict[str, str] | None = None, project_clone: str | None = None,
    clone_dir: str = "/state/projects/p/project-clone", volume: str = "scoter-p-store",
) -> UserConfig:
    """Enough of a universal configuration file for the `.env` file and the clone."""
    container: dict[str, object] = {"dev-container-path": "./env"}
    if project_clone is not None:
        container["mounts"] = {"project-clone": project_clone}
    return UserConfig({
        "active-config-locations": {},
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
        },
    })


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


class ProjectCloneTest(unittest.TestCase):
    """Making the project's clone."""

    def setUp(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.tmp = Path(os.path.realpath(directory))
        self.clone = self.tmp / "state" / "project-clone"
        self.clone.parent.mkdir()

    def make(self, clean: bool = False) -> Path:
        config = suc(clone_dir=str(self.clone))
        with contextlib.redirect_stdout(io.StringIO()):
            return main_run._setup_project_clone(config, None, clean, True)

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

    def test_file_refused(self) -> None:
        self.clone.write_text("x", encoding="utf-8")
        with self.assertRaises(ConfigErr):
            self.make()


if __name__ == "__main__":
    unittest.main()
