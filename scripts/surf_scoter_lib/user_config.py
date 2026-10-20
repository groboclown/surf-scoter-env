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
# The SUC's record of the project file, as written.
CURRENT_PROJECT_SETUP = "current-project-setup"

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
        """A section's `paths` location; None when it isn't set, or is null."""
        loc = self.__find(jdata.find_str, "resolved", section, "paths", key)
        return Path(loc) if loc else None

    def require_value(self, section: str, key: str) -> str:
        """Load a value (as a string) that might exist."""
        return self.__req_str("resolved", section, "values", key)

    def active_location(self, name: str) -> Path | None:
        """Read an active location path; None when it isn't set.

        The generator writes every location, with null for the ones not set.
        """
        locations = self._config.get("active-config-locations")
        if locations is None:
            return None
        if not isinstance(locations, dict):
            raise ConfigErr(
                "bad universal configuration file: active-config-locations: expected a mapping"
            )
        val = locations.get(name)
        if val is None:
            return None
        if not isinstance(val, str):
            raise ConfigErr(
                f"bad universal configuration file: active-config-locations / {name}: "
                f"expected string, found {type(val)}"
            )
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
        """Get a key of the project file, as written (the SUC's `current-project-setup`)."""
        return self.__find(jdata.find_str, CURRENT_PROJECT_SETUP, *key)

    def project_bridge_commands(self) -> list[str]:
        """The names of the commands the host runs for the container (`host-brokered`), once each."""
        ret: list[str] = []
        for path in self.__req_list_keys("host-brokered"):
            name = self.__req_str(*path, "command")
            if name not in ret:
                ret.append(name)
        return ret

    def setup_programs(self) -> list[tuple[str, str, str, str]]:
        """Every setup file's `programs` entries, as written.

        Each is (file, section, key, value), highest priority file first.
        """
        ret: list[tuple[str, str, str, str]] = []
        for file_path in self.__req_list_keys("setup", "files"):
            file = self.__req_str(*file_path, "file")
            for section_path in self.__req_dict_keys(*file_path, "sections"):
                section = str(section_path[-1])
                for key_path in self.__req_dict_keys(*section_path, "programs"):
                    value = self.__req_str(*key_path)
                    ret.append((file, section, str(key_path[-1]), value))
        return ret

    def host_command_checks(self) -> dict[str, jdata.DictJsonData]:
        """The setup's `host-commands` checks, by `section:key`, as the generator combined them."""
        # A universal file from before the checks has none.
        return {
            str(path[-1]): self.__req_dict(*path)
            for path in self.__find(jdata.find_dict_keys, "host-command-checks", "checks")
        }

    def host_command_notices(self) -> list[str]:
        """What the generator noticed about the setup's `host-commands` checks, as text.

        Such as a check a setup file removes or replaces, which then misses what
        the lower priority file adds to it later, or an override that does nothing.
        Each notice in the file also has an `id`, `parameters` (for translating it)
        and `sources` (the setup files it's about); this gives its `message`.
        """
        return [
            self.__req_str(*path, "message")
            for path in self.__find(jdata.find_list_keys, "host-command-checks", "notices")
        ]

    def project_setup(self) -> jdata.DictJsonData | None:
        """The project file, as written (`current-project-setup`); None without a project."""
        return self.__find(jdata.find_dict, CURRENT_PROJECT_SETUP)

    def project_commands(self) -> list[jdata.DictJsonData]:
        """The project file's `commands`, as written."""
        return [
            self.__req_dict(*path)
            for path in self.__find(jdata.find_list_keys, CURRENT_PROJECT_SETUP, "commands")
        ]

    def project_env_allow(self) -> list[str]:
        """The project file's top-level `env` `allow` patterns, as written."""
        return self.__find(jdata.find_str_list, CURRENT_PROJECT_SETUP, "env", "allow")

    def dev_container(self) -> jdata.DictJsonData | None:
        """The dev container's `dev-env.yaml`, merged with its parents; None without a project."""
        return self.__find(jdata.find_dict, "dev-container")

    def has_dev_container(self) -> bool:
        """Whether the file has the dev container's `dev-env.yaml` (it does with a project)."""
        return self.dev_container() is not None

    def egress_rules(self) -> tuple[list[jdata.DictJsonData], str]:
        """The egress rules, and where they came from.

        The accepted ones (`egress.rules`, source `accepted`); or, without
        accepted settings, the project file's own (source `project`), unreviewed.
        """
        if self.__find(jdata.find_dict, "egress") is not None:
            path: tuple[str, ...] = ("egress", "rules")
            source = "accepted"
        else:
            path = (CURRENT_PROJECT_SETUP, "egress")
            source = "project"
        return [self.__req_dict(*key) for key in self.__find(jdata.find_list_keys, *path)], source

    def host_command_map(self) -> dict[str, str | None]:
        """Each command name the setup's checks map, with its program (None when it isn't there)."""
        return {
            str(path[-1]): self.__find(jdata.find_str, *path, "program")
            for path in self.__find(jdata.find_dict_keys, "host-command-checks", "command-map")
        }

    def value_list(self, section: str, key: str) -> list[str]:
        """A section's `value-lists` list; empty when it isn't set."""
        return self.__find(jdata.find_str_list, "resolved", section, "value-lists", key)

    def optional_path_list(self, section: str, key: str) -> list[Path]:
        """A section's `path-lists` list; empty when it isn't set, or has no value."""
        return [
            Path(p) for p in self.__find(jdata.find_str_list, "resolved", section, "path-lists", key)
        ]

    def optional_value(self, section: str, key: str) -> str | None:
        """A section's `values` text; None when it isn't set, or is null."""
        return self.__find(jdata.find_str, "resolved", section, "values", key)

    def optional_int_value(self, section: str, key: str) -> int | None:
        """A section's `values` integer; None when it isn't set, or is null."""
        return self.__find(jdata.find_int, "resolved", section, "values", key)

    def has_section(self, section: str) -> bool:
        """Whether the setup has the section."""
        return self.__find(jdata.find_dict, "resolved", section) is not None

    def raw(self, *path: jdata.JsonPathPart) -> jdata.JsonData:
        """The file's data at the path, as it is; None when it's missing or null.

        For data kept or compared whole, such as the setup values an acceptance
        records; read values the tools use through the typed accessors.
        """
        return self.__find(jdata.find, *path)

    def resolved_program(self, section: str, key: str) -> str | None:
        """The program found for a `programs` entry; None if it wasn't found.

        The generator writes null for a program it didn't find.
        """
        val: jdata.JsonData = self._config
        for part in ("resolved", section, "programs", key):
            if not isinstance(val, dict):
                return None
            val = val.get(part)
        if val is None or isinstance(val, str):
            return val
        raise ConfigErr(
            f"bad universal configuration file: resolved / {section} / programs / {key}: "
            f"expected string, found {type(val)}"
        )

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

    def __find[T](
        self, lookup: Callable[..., T], *path: jdata.JsonPathPart,
    ) -> T:
        """One of jdata's `find_*` lookups (missing or null is fine), as a ConfigErr if it fails."""
        return UserConfig.__wrap(lambda: lookup(self._config, *path))

    def __req_dict_keys(self, *path: jdata.JsonPathPart) -> Sequence[jdata.JsonPath]:
        return UserConfig.__wrap(lambda: jdata.opt_dict_keys(self._config, *path))
    @staticmethod
    def __wrap[T](call: Callable[[], T]) -> T:
        try:
            return call()
        except Exception as err:
            raise ConfigErr(f"bad universal configuration file: {err}") from err
