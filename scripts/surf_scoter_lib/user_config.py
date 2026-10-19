#SPDX:MIT
"""The Surf Scoter Universal Configuration file handler."""
import json
import os
import subprocess  # nosec: B404  # We know the potential issues with subprocess.
from collections.abc import Callable, Sequence
from pathlib import Path

from . import jdata
from .err import ConfigErr

# The few hard-coded items.
VCS_SECTION_SUFFIX = "-vcs"

# ---------------------------------------------------------------------
# Configuration Understanding

class UserConfig:
    """Manages the information in the Scoter Universal Configuration (SUC) file."""

    def __init__(self, universal_config: jdata.DictJsonData) -> None:
        self._config = universal_config

    @staticmethod
    def read(file: Path) -> "UserConfig":
        """Read the SUC file at the path."""
        with open(file, "rb") as inp:
            data: jdata.JsonData = json.load(inp)
            return UserConfig(UserConfig.__wrap(lambda: jdata.req_dict(data)))

    def get_vcs_section(self) -> str | None:
        """Get the VCS section for the VCS name.
        
        Raises a ConfigErr if the VCS is not registered.
        """
        name = self.project_str("vcs")
        if not name:
            return None
        return self.get_vcs_section_for(name)

    def get_vcs_section_for(self, name: str) -> str:
        """Get the VCS section for the VCS name.
        
        Raises a ConfigErr if the VCS is not registered.
        """
        ret = name + VCS_SECTION_SUFFIX
        # ensure it exists
        if self.__opt_dict("resolved", ret) is None:
            raise ConfigErr(f"VCS provider '{name}' not supported with the current configuration")
        return ret

    def search_path(self, section: str, key: str, file: str | None, is_dir: bool) -> Path | None:
        """Search the path list at the section/key for the file.
        
        If the file is None, then this just tries to find the first of the path list that
        exists.  If the section/key does not exist, this raises a ConfigErr.
        """
        for path in self.__req_list_keys("resolved", section, "path-lists", key):
            loc = self.__req_str(*path)
            if file is None:
                if (is_dir and os.path.isdir(loc)) or (not is_dir and os.path.isfile(loc)):
                    return Path(loc)
            else:
                loc = os.path.join(loc, file)
                if (is_dir and os.path.isdir(loc)) or (not is_dir and os.path.isfile(loc)):
                    return Path(loc)
        return None

    def get_dist_bin_dir(self) -> Path:
        """Get the distribution's bin directory."""
        return self.require_active_location("distribution-bin-dir")

    def require_path(self, section: str, key: str) -> Path:
        """Load a path that must exist."""
        return Path(self.__req_str("resolved", section, "paths", key))

    def optional_path(self, section: str, key: str) -> Path | None:
        """Load a path that might exist."""
        loc = self.__opt_str("resolved", section, "paths", key)
        if loc:
            return Path(loc)
        return None

    def require_value(self, section: str, key: str) -> str:
        """Load a value (as a string) that might exist."""
        return self.__req_str("resolved", section, "values", key)

    def active_location(self, name: str) -> Path | None:
        """Read an active location path."""
        val = self.__opt_str("active-config-locations", name)
        if val is None:
            return None
        return Path(val)

    def require_active_location(self, name: str) -> Path:
        """Read an active location path."""
        return Path(self.__req_str("active-config-locations", name))

    def set_active_location(self, name: str, path: Path) -> None:
        """Overwrite the active location."""
        locs = self.__req_dict("active-config-locations")
        locs[name] = str(path)

    def require_run(
        self, section: str, run: str, template: dict[str, str] | None = None
    ) -> list[str]:
        """Return the required run command."""
        args: list[str] = []
        for path in self.__req_list_keys("resolved", section, "runs", run):
            arg = self.__req_str(*path)
            for key,val in (template or {}).items():
                arg = arg.replace("{x:" + key + "}", val)
            args.append(arg)
        return args

    def run_command(
        self,
        section: str,
        run: str,
        /, *,
        cwd: Path | None = None,
        check: bool = True,
        capture_stdout: bool = True,
        capture_stderr: bool = False,
        template: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run the given command.
        
        This will return the completed process with stdout captured.  stderr will
        forward to the user.
        """
        cmd = self.require_run(section, run, template)
        print(f"Running {cmd[0]}")
        print(f"DEBUG {cmd}")
        return subprocess.run(  # nosec: B603  # Input validated on the call.
            cmd,
            stdout=subprocess.PIPE if capture_stdout else None,
            stderr=subprocess.PIPE if capture_stderr else None,
            check=check,
            cwd=cwd,
            shell=False,
        )

    def dev_container_sshd_port_env(self) -> str | None:
        """Get the sshd-port-env from the dev-container."""
        return self.__opt_str("dev-container", "sshd-port-env")

    def dev_container_sshd_port(self) -> str | None:
        """Get the sshd-port from the dev-container."""
        return self.__opt_str("dev-container", "sshd-port")

    def set_dev_container_sshd_port(self, value: str) -> None:
        """Set the ssh-port in the dev-container, if it's unset."""
        current = self.dev_container_sshd_port()
        if current is not None:
            raise RuntimeError("tried to set the ssh-port when it's already set")
        dev_con = jdata.req_dict(self._config, "dev-container")
        dev_con["sshd-port"] = int(value)

    def dev_container_str(self, key: str) -> str:
        """Get the dev-container key."""
        val = self.__opt_str("dev-container", key)
        if val is None:
            raise ConfigErr(f"dev-env.yaml must provide a value at 'dev-container/{key}'")
        return val

    def dev_container_mounts(self) -> dict[str, str]:
        """Get the dev-container volume mount points."""
        ret: dict[str, str] = {}
        for path in self.__req_dict_keys("dev-container", "mounts"):
            ret[str(path[-1])] = self.__req_str(*path)
        return ret

    def dev_container_parent_builds(self) -> list[tuple[str, Path]]:
        """Get the dev-container's local parent images to build, lowest first, as (tag, dir)."""
        ret: list[tuple[str, Path]] = []
        for path in self.__req_list_keys("dev-container", "parent-builds"):
            tag = self.__req_str(*path, "tag")
            dirname = self.__req_str(*path, "dir")
            ret.append((tag, Path(dirname)))
        return ret

    def project_str(self, *key: jdata.JsonPathPart) -> str | None:
        """Get the project key."""
        val = self.__opt_str("project", *key)
        if val is None:
            return None
        return str(val)

    def project_bridge_commands(self) -> list[str]:
        """Get the list of bridge commands."""
        ret: list[str] = []
        for path in self.__req_list_keys("project", "commands"):
            ret.append(self.__req_str(*path, "command"))
        return ret

    def save(self, outfile: str | Path) -> None:
        """Save the current configuration to the given file."""
        with open(outfile, "w", encoding="utf-8") as out:
            json.dump(self._config, out)

    def dumps(self) -> str:
        """Output the current configuration to a string."""
        return json.dumps(self._config)

    def __req_str(self, *path: jdata.JsonPathPart) -> str:
        return UserConfig.__wrap(lambda: jdata.req_str(self._config, *path))

    def __opt_str(self, *path: jdata.JsonPathPart) -> str | None:
        return UserConfig.__wrap(lambda: jdata.opt_str(self._config, *path))

    def __req_list_keys(self, *path: jdata.JsonPathPart) -> Sequence[jdata.JsonPath]:
        return UserConfig.__wrap(lambda: jdata.opt_list_keys(self._config, *path))

    def __req_dict(self, *path: jdata.JsonPathPart) -> jdata.DictJsonData:
        return UserConfig.__wrap(lambda: jdata.req_dict(self._config, *path))

    def __opt_dict(self, *path: jdata.JsonPathPart) -> jdata.DictJsonData | None:
        return UserConfig.__wrap(lambda: jdata.opt_dict(self._config, *path))

    def __req_dict_keys(self, *path: jdata.JsonPathPart) -> Sequence[jdata.JsonPath]:
        return UserConfig.__wrap(lambda: jdata.opt_dict_keys(self._config, *path))
    @staticmethod
    def __wrap[T](call: Callable[[], T]) -> T:
        try:
            return call()
        except Exception as err:
            raise ConfigErr(f"bad universal configuration file: {err}") from err
