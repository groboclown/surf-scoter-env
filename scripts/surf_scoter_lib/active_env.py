#SPDX:MIT
"""Handler for the active environments file.

This sits in the user's state directory, which allows the tools to know
about parallel executions, so they don't step on each other.

It also provides sufficient information for the user to debug and understand
what's running on their system, and for `scoter kill` to tear down an
environment whose `scoter run` is gone.

Each change is made under an operating system lock (`fileutil.exclusive_lock`),
held only while the file is read, changed and written: a crashed run never
leaves it held.  The file holds a copy of each environment's whole universal
configuration file, so it's written private (`fileutil.write_private`).
"""
import contextlib
import datetime
import json
import os
import stat
import sys
from collections.abc import Generator
from dataclasses import dataclass, replace
from pathlib import Path

from . import fileutil, jdata
from .err import ActiveEnvErr, LockTimeoutErr

MAX_FILE_SIZE = 1024 * 1024 * 10  # 10 MB
LOCK_TIMEOUT_SECONDS = 30.0

# ---------------------------------------------------------------------
# Active container record

@dataclass(frozen=True)
class EnvironmentDesc:
    """Full description of a dev environment.

    This contains "enough" information to help the tool tear down the
    environment, even if it's partially already removed.  A reservation
    (before the run has built anything) has the host, port, user, project
    and process, and empty files.
    """

    # Data extracted from the SUC used to execute the environment.
    host_prefix: str
    host_suffix: str
    dev_env_name: str
    firewall_router_name: str
    network_gateway_name: str
    project_dir: str
    resources_dir: str
    workspace_dir: str
    setup_dir: str
    compose_file: str
    compose_env_file: str
    dev_env_ssh_port: str
    dev_env_ssh_user: str
    created_at: str

    # Captures all the data when launched, and allows for
    # teardown discovery and user debugging.
    suc: jdata.DictJsonData

    # The `scoter run` process; 0 when it isn't known.
    pid: int = 0

    @property
    def host(self) -> str:
        """The environment's host name."""
        return self.host_prefix + self.host_suffix

    @staticmethod
    def from_json(file: Path, data: jdata.JsonData) -> "EnvironmentDesc":
        """Extract this from JSON formatted data."""
        try:
            pid = jdata.find(data, "pid")
            return EnvironmentDesc(
                host_prefix=jdata.req_str(data, "host_prefix"),
                host_suffix=jdata.req_str(data, "host_suffix"),
                dev_env_name=jdata.req_str(data, "dev_env_name"),
                firewall_router_name=jdata.req_str(data, "firewall_router_name"),
                network_gateway_name=jdata.req_str(data, "network_gateway_name"),
                project_dir=jdata.req_str(data, "project_dir"),
                resources_dir=jdata.req_str(data, "resources_dir"),
                workspace_dir=jdata.req_str(data, "workspace_dir"),
                setup_dir=jdata.req_str(data, "setup_dir"),
                compose_file=jdata.req_str(data, "compose_file"),
                compose_env_file=jdata.req_str(data, "compose_env_file"),
                dev_env_ssh_port=jdata.req_str(data, "dev_env_ssh_port"),
                dev_env_ssh_user=jdata.req_str(data, "dev_env_ssh_user"),
                created_at=jdata.req_str(data, "created_at"),
                suc=jdata.req_dict(data, "suc"),
                pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else 0,
            )
        except Exception as err:
            raise ActiveEnvErr(f"{file}: encountered format issues ({err})") from err

    def to_json(self) -> jdata.JsonData:
        """Serialize this data."""
        return {
            "host_prefix": self.host_prefix,
            "host_suffix": self.host_suffix,
            "dev_env_name": self.dev_env_name,
            "firewall_router_name": self.firewall_router_name,
            "network_gateway_name": self.network_gateway_name,
            "project_dir": self.project_dir,
            "resources_dir": self.resources_dir,
            "workspace_dir": self.workspace_dir,
            "setup_dir": self.setup_dir,
            "compose_file": self.compose_file,
            "compose_env_file": self.compose_env_file,
            "dev_env_ssh_port": self.dev_env_ssh_port,
            "dev_env_ssh_user": self.dev_env_ssh_user,
            "created_at": self.created_at,
            "suc": self.suc,
            "pid": self.pid,
        }


class ActiveEnvironmentSet:
    """A file record of the active containers.

    Read it with `read`, or change it with `locked`, which writes it back
    when its block ends without an error.
    """

    ENVIRON_IDS = "abcdefghijklmnopqrstuvwxyz"

    def __init__(self, *, file: Path, environs: list[EnvironmentDesc]) -> None:
        self._file = file
        self.environs: dict[str, EnvironmentDesc] = {}
        for env in environs:
            if env.host_suffix in self.environs:
                raise ActiveEnvErr(
                    f"bad active environment config ({file}): "
                    f"duplicate id {env.host_suffix}"
                )
            self.environs[env.host_suffix] = env

    def to_json(self) -> jdata.JsonData:
        """Serialize this data."""
        return [e.to_json() for e in self.environs.values()]

    def reserve_environ(
        self, host_prefix: str, *, port: str = "", user: str = "", project_dir: str = "",
    ) -> str:
        """Reserve a host environment; its suffix.

        The port and user are recorded now, so another run choosing its port
        sees this one's.
        """
        for eid in ActiveEnvironmentSet.ENVIRON_IDS:
            if eid not in self.environs:
                self.environs[eid] = EnvironmentDesc(
                    host_prefix=host_prefix,
                    host_suffix=eid,
                    dev_env_name="",
                    firewall_router_name="",
                    network_gateway_name="",
                    project_dir=project_dir,
                    resources_dir="",
                    workspace_dir="",
                    setup_dir="",
                    compose_file="",
                    compose_env_file="",
                    dev_env_ssh_port=port,
                    dev_env_ssh_user=user,
                    created_at=datetime.datetime.now().isoformat(),
                    suc={},
                    pid=os.getpid(),
                )
                return eid
        raise ActiveEnvErr("Too many registered environments.")

    def update_environ(self, env: EnvironmentDesc) -> None:
        """Update the environment description."""
        if env.host_suffix not in self.environs:
            raise ActiveEnvErr(
                f"environment {env.host} isn't registered any more; "
                "was it stopped with `scoter kill`?"
            )
        self.environs[env.host_suffix] = replace(env, pid=env.pid or self.environs[env.host_suffix].pid)

    def get_active_host_ids(self) -> list[str]:
        """Get all the active host IDs."""
        return list(self.environs.keys())

    def get_active_host_ids_for_prefix(self, prefix: str) -> list[str]:
        """Get all the active host IDs for the given host prefix."""
        return [env.host_suffix for env in self.environs.values() if env.host_prefix == prefix]

    def get_environ_by_host(self, host: str) -> EnvironmentDesc | None:
        """Get the environment description by the host name."""
        for env in self.environs.values():
            if env.host == host:
                return env
        return None

    def environs_for_project(self, project_dir: str) -> list[EnvironmentDesc]:
        """The environments running for the project directory (by its real path)."""
        real = os.path.realpath(project_dir)
        return [
            env for env in self.environs.values()
            if env.project_dir and os.path.realpath(env.project_dir) == real
        ]

    def rm_environ_by_host(self, host: str) -> bool:
        """Attempt to remove the host from the active list."""
        env = self.get_environ_by_host(host)
        if env:
            del self.environs[env.host_suffix]
            return True
        return False

    @staticmethod
    def read(file: Path) -> "ActiveEnvironmentSet":
        """Read the active environment set, without changing it."""
        return ActiveEnvironmentSet(file=file, environs=ActiveEnvironmentSet._read_contents(file))

    @staticmethod
    @contextlib.contextmanager
    def locked(
        file: Path, lock_file: Path, timeout: float = LOCK_TIMEOUT_SECONDS,
    ) -> Generator["ActiveEnvironmentSet"]:
        """The active environment set, to change while in the block, under the lock.

        It's written back, private, when the block ends without an error;
        with one, nothing changes.
        """
        # The state directory, made private, the first time.
        fileutil.private_dir(lock_file.parent, ())
        _allow_old_lock_file(lock_file)

        def waiting() -> None:
            print(
                "Waiting for another Surf Scoter command to finish with the active environments...",
                file=sys.stderr,
            )

        try:
            with fileutil.exclusive_lock(lock_file, timeout=timeout, on_wait=waiting):
                envs = ActiveEnvironmentSet.read(file)
                yield envs
                envs._write()
        except LockTimeoutErr as err:
            raise ActiveEnvErr(
                f"{err}.  If no Surf Scoter command is starting or stopping an environment, "
                "something else holds it."
            ) from err

    def _write(self) -> None:
        data = (json.dumps(self.to_json()) + "\n").encode("utf-8")
        fileutil.write_private(self._file.parent, (self._file.name,), data)

    @staticmethod
    def _read_contents(file: Path) -> list[EnvironmentDesc]:
        try:
            fd = os.open(file, os.O_RDONLY | fileutil.O_NOFOLLOW | fileutil.O_BINARY)
        except FileNotFoundError:
            return []
        except OSError as err:
            raise ActiveEnvErr(f"{file}: can't be read ({err.strerror})") from err
        with os.fdopen(fd, "rb") as inp:
            if not stat.S_ISREG(os.fstat(inp.fileno()).st_mode):
                raise ActiveEnvErr(f"{file}: isn't a regular file")
            raw = inp.read(MAX_FILE_SIZE + 1)
        # Put a cap on file size.
        if len(raw) > MAX_FILE_SIZE:
            raise ActiveEnvErr(
                f"{file}: file too large.  You have too many parallel environments "
                f"marked as running.  Either environments haven't cleaned up right, "
                f"or you need to reconsider the choices that brought you to this point."
            )
        text = raw.decode("utf-8", errors="replace")
        if not text.strip():
            return []
        try:
            data = jdata.req_list(jdata.json_from_str(text))
        except (json.JSONDecodeError, TypeError, KeyError, ValueError) as err:
            raise ActiveEnvErr(f"wrong formatted active environment set {file}") from err
        return [EnvironmentDesc.from_json(file, e) for e in data]


def _allow_old_lock_file(lock_file: Path) -> None:
    """An earlier version left its lock file read-only; make one of yours writable again."""
    try:
        st = os.lstat(lock_file)
    except FileNotFoundError:
        return
    if (
        stat.S_ISREG(st.st_mode) and not st.st_mode & stat.S_IWUSR
        and (not hasattr(os, "getuid") or st.st_uid == os.getuid())
    ):
        os.chmod(lock_file, fileutil.MODE_USER_RW)
