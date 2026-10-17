#SPDX:MIT
"""Handler for the active environments file.

This sits in the user's scoter home directory, which allows the tools to
know about parallel executions, so they don't step on each other.

It also provides sufficient information for the user to debug and understand
what's running on their system.
"""
from dataclasses import dataclass
import datetime
import json
from pathlib import Path

from . import jdata
from . import fileutil
from .err import ActiveEnvErr

MAX_FILE_SIZE = 1024 * 1024 * 10  # 10 MB

# ---------------------------------------------------------------------
# Active container record

@dataclass(frozen=True)
class EnvironmentDesc:
    """Full description of a dev environment.

    This contains "enough" information to help the tool tear down the
    environment, even if it's partially already removed.
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

    @staticmethod
    def from_json(file: Path, data: jdata.JsonData) -> "EnvironmentDesc":
        """Extract this from JSON formatted data."""
        try:
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
        }


class ActiveEnvironmentSet:
    """A file record of the active containers."""

    ENVIRON_IDS = "abcdefghijklmnopqrstuvwxyz"

    def __init__(
        self,
        *,
        file: Path,
        lock_file: Path | None,
        orig: str,
        environs: list[EnvironmentDesc],
    ) -> None:
        self._file = file
        self._lock = lock_file
        self._orig = orig
        self.environs: dict[str, EnvironmentDesc] = {}
        for env in environs:
            if env.host_suffix in self.environs:
                raise ActiveEnvErr(
                    f"bad active environment config ({file}): "
                    f"duplicate id {env.host_suffix}"
                )
            self.environs[env.host_suffix] = env

    def __del__(self) -> None:
        if self._lock:
            print(
                f"Warning: the active environment ({self._file}) is left in a locked state.  "
                f"You'll need to manually clean it up."
            )

    def to_json(self) -> jdata.JsonData:
        """Serialize this data."""
        return [e.to_json() for e in self.environs.values()]

    def reserve_environ(self, host_prefix: str) -> str:
        """Reserve a host environment."""
        if self._lock is None:
            raise RuntimeError("read-only")
        for eid in ActiveEnvironmentSet.ENVIRON_IDS:
            if eid not in self.environs:
                self.environs[eid] = EnvironmentDesc(
                    host_prefix=host_prefix,
                    host_suffix=eid,
                    dev_env_name="",
                    firewall_router_name="",
                    network_gateway_name="",
                    project_dir="",
                    resources_dir="",
                    workspace_dir="",
                    setup_dir="",
                    compose_file="",
                    compose_env_file="",
                    dev_env_ssh_port="",
                    dev_env_ssh_user="",
                    created_at=datetime.datetime.now().isoformat(),
                    suc={},
                )
                return eid
        raise ActiveEnvErr("Too many registered environments.")

    def update_environ(self, env: EnvironmentDesc) -> None:
        """Update the environment description."""
        if self._lock is None:
            raise RuntimeError("read-only")
        if env.host_suffix not in self.environs:
            raise RuntimeError(f"environment ID {env.host_suffix} not registered")
        self.environs[env.host_suffix] = env

    def get_active_host_ids(self) -> list[str]:
        """Get all the active host IDs."""
        return list(self.environs.keys())

    def get_active_host_ids_for_prefix(self, prefix: str) -> list[str]:
        """Get all the active host IDs for the given host prefix."""
        ret: list[str] = []
        for env in self.environs.values():
            if env.host_prefix == prefix:
                ret.append(env.host_suffix)
        return ret

    def get_environ_by_host(self, host: str) -> EnvironmentDesc | None:
        """Get the environment description by the host name."""
        for env in self.environs.values():
            if env.host_prefix + env.host_suffix == host:
                return env
        return None

    def rm_environ_by_host(self, host: str) -> bool:
        """Attempt to remove the host from the active list."""
        if self._lock is None:
            raise RuntimeError("read-only")
        env = self.get_environ_by_host(host)
        if env:
            del self.environs[env.host_suffix]
            return True
        return False

    @staticmethod
    def open_readonly(file: Path) -> "ActiveEnvironmentSet":
        """Read the active environment set."""
        orig, environs = ActiveEnvironmentSet._read_contents(file)
        return ActiveEnvironmentSet(
            file=file,
            lock_file=None,
            orig=orig,
            environs=environs,
        )

    @staticmethod
    def open(
        file: Path, lock_file: Path
    ) -> "ActiveEnvironmentSet":
        """Open the active environment set for writing."""
        if lock_file.is_file():
            # Something else owns the lock.
            # This could retry in a loop, but the real issue is either
            # the files are in a bad state, or the user tried starting
            # up a bunch of environments too quickly.  Blame it on them.
            raise ActiveEnvErr(
                f"The active environment lock is in place ({lock_file}).  "
                f"That means either something crashed while working on an environment, "
                f"or you tried to start this environment while another is starting or stopping."
            )
        if not file.is_file():
            # No file yet.  Mark the lock and return empty.
            lock_file.write_text("[]", encoding="utf-8")
            fileutil.touch(lock_file, 0o400)
            return ActiveEnvironmentSet(
                file=file,
                lock_file=lock_file,
                orig="[]",
                environs=[],
            )
        # Setup the lock and return the contents.
        # If some OS problem happens when performing the file manipulations,
        # don't correct the lock - that can mean someone stole it from us.
        try:
            fileutil.touch(lock_file, fileutil.MODE_USER_RW)  # ensure it exists and is writable.
            orig, environs = ActiveEnvironmentSet._read_contents(file)
            lock_file.write_text(orig, encoding="utf-8")
            fileutil.touch(lock_file, fileutil.MODE_USER_R_ONLY)  # make read-only
            return ActiveEnvironmentSet(
                file=file,
                lock_file=lock_file,
                orig=orig,
                environs=environs,
            )
        except OSError as err:
            # Could not capture the lock.
            raise ActiveEnvErr(
                f"The active environment lock is in place ({lock_file}).  "
                f"That means either something crashed while working on an environment, "
                f"or you tried to start this environment while another is starting or stopping."
            ) from err

    @staticmethod
    def _read_contents(file: Path) -> tuple[str, list[EnvironmentDesc]]:
        if file.is_file():
            # Put a cap on file size.
            if file.stat().st_size > MAX_FILE_SIZE:
                raise ActiveEnvErr(
                    f"{file}: file too large.  You have too many parallel environments "
                    f"marked as running.  Either environments haven't cleaned up right, "
                    f"or you need to reconsider the choices that brought you to this point."
                )
            with open(file, "r", encoding="utf-8") as inp:
                text = inp.read()
            try:
                data = jdata.req_list(jdata.json_from_str(text))
            except (json.JSONDecodeError, TypeError, KeyError, ValueError) as err:
                raise ActiveEnvErr(f"wrong formatted active environment set {file}") from err
            return (text, [EnvironmentDesc.from_json(file, e) for e in data])
        return ("", [])

    def close(self) -> None:
        """Close without saving, and disallow additional save actions."""
        # Note: this is explicitly not run at __del__() time.
        # This allows for leaving things in a reserved state for when something went sideways.
        if self._lock:
            if self._is_lock_owned():
                fileutil.touch(self._lock, fileutil.MODE_USER_RW)  # turn writable
                fileutil.rm(self._lock)
            else:
                print(
                    f"Looks like someone stole the lock from us ({self._lock}).  "
                    f"Not touching it."
                )
            self._lock = None

    def _is_lock_owned(self) -> bool:
        """Check if the lock file is still the one we think it is."""
        if not self._lock:
            # Read-only access.
            return False
        if self._lock.is_file():
            lock_data = ActiveEnvironmentSet.open_readonly(self._lock)
            # If the contents match, then consider it still owned by us.
            return lock_data._orig == self._orig
        # Something stole the lock from us.
        return False

    def save(self, close: bool = True) -> bool:
        """Save the current file."""
        # Inspect the lock file.  It should match up with expectations.
        if not self._lock:
            raise RuntimeError("read-only")
        if not self._is_lock_owned():
            return False
        # Write the data.
        with open(self._file, "w", encoding="utf-8") as out:
            json.dump(self.to_json(), out)
        if close:
            fileutil.touch(self._lock, fileutil.MODE_USER_RW)  # turn writable
            fileutil.rm(self._lock)
            self._lock = None
        return True

