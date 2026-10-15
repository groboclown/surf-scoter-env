#!/usr/sbin/env python3
"""The surf-scoter user tool."""
# Compatibility: Python 3.12+
import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import datetime
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

JsonData = str | int | float | bool | None | list["JsonData"] | dict[str, "JsonData"]
DictJsonData = dict[str, JsonData]
ListJsonData = list[JsonData]
JsonPathPart = str | int


# The few hard-coded items.
DISTRIBUTION_BIN_DIR = "bin"
SCOTER_HOST_EXEC = "scoter-host"
GEN_SCOTER_UC_EXEC = "gen-scoter-uc"
VCS_SECTION_SUFFIX = "-vcs"
DOCKER_COMPOSE_FILENAME = "docker-compose.yaml"
PUB_KEY_EXT = ".pub"
COMPOSE_ENV_FILENAME = ".env"
CONTAINER_HOST_KEY_FILE_NAME = "ssh_host_ed25519_key"
AUTHORIZED_KEYS_FILE_NAME = "authorized_keys"
ALLOWED_DOMAINS_FILE_NAME = "allowed-domains.txt"
BRIDGE_COMMANDS_FILE_NAME = "bridge-cmds.txt"
TOP_PORT_NUMBER = 32767
CONTAINER_START_TIME_SEC = 60.0


# ---------------------------------------------------------------------
# Configuration Understanding

class ConfigErr(Exception):
    """A configuration problem happened."""


class UserConfig:
    """Manages the information in the Scoter Universal Configuration (SUC) file."""

    def __init__(self, universal_config: DictJsonData, bin_dir: Path) -> None:
        self._config = universal_config
        self.bin_dir = bin_dir

    def search_path(self, section: str, key: str, file: str | None, is_dir: bool) -> Path | None:
        """Search the path list at the section/key for the file.
        
        If the file is None, then this just tries to find the first of the path list that
        exists.  If the section/key does not exist, this raises a ConfigErr.
        """
        for path,loc in self.__require_list_path("resolved", section, "path-lists", key):
            if not isinstance(loc, str):
                raise self.__key_err(path, "expected string")
            if file is None:
                if (is_dir and os.path.isdir(loc)) or (not is_dir and os.path.isfile(loc)):
                    return Path(loc)
            else:
                loc = os.path.join(loc, file)
                if (is_dir and os.path.isdir(loc)) or (not is_dir and os.path.isfile(loc)):
                    return Path(loc)
        return None

    def require_path(self, section: str, key: str) -> Path:
        """Load a path that must exist."""
        loc = self.__opt_str("resolved", section, "paths", key)
        if loc is None:
            raise self.__key_err(("resolved", section, "paths", key), "path must exist")
        return Path(loc)

    def optional_path(self, section: str, key: str) -> Path | None:
        """Load a path that might exist."""
        loc = self.__opt_str("resolved", section, "paths", key)
        if loc:
            return Path(loc)
        return None

    def require_value(self, section: str, key: str) -> str:
        """Load a value (as a string) that might exist."""
        val = self.__opt_str("resolved", section, "values", key)
        if val is None:
            raise self.__key_err(("resolved", section, "values", key), "value must exist")
        return str(val)

    def active_location(self, name: str) -> Path | None:
        """Read an active location path."""
        val = self.__opt_str("active-config-locations", name)
        if val is None:
            return None
        return Path(str(val))

    def require_active_location(self, name: str) -> Path:
        """Read an active location path."""
        val = self.__opt_str("active-config-locations", name)
        if val is None:
            raise self.__key_err(("active-config-locations", "name"), "value must exist")
        return Path(str(val))

    def set_active_location(self, name: str, path: Path) -> None:
        """Overwrite the active location."""
        locs = self._config["active-config-locations"]
        assert isinstance(locs, dict)
        locs[name] = str(path)

    def require_run(
        self, section: str, run: str, template: dict[str, str] | None = None
    ) -> list[str]:
        """Return the required run command."""
        raw_args = self.__require_list("resolved", section, "runs", run)
        args: list[str] = []
        for arg in raw_args:
            if not isinstance(arg, str):
                raise self.__key_err(
                    ("resolved", section, "runs", run),
                    "runs must be a list of strings",
                )
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
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE if capture_stdout else None,
            stderr=subprocess.PIPE if capture_stderr else None,
            check=check,
            cwd=cwd,
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
        dev_con = self._config["dev-container"]
        assert isinstance(dev_con, dict)
        dev_con["sshd-port"] = int(value)

    def dev_container_str(self, key: str) -> str:
        """Get the dev-container key."""
        val = self.__opt_str("dev-container", key)
        if val is None:
            raise self.__key_err(("dev-container", key), "dev-env.yaml must provide this value")
        return val

    def dev_container_mounts(self) -> dict[str, str]:
        """Get the dev-container volume mount points."""
        val, exists = self.__get(("dev-container", "mounts"))
        if not exists or not isinstance(val, dict):
            print(f"DEBUG container mounts should be dict, found {val} ({type(val)})")
            return {}
        return val

    def dev_container_parent_builds(self) -> list[tuple[str, Path]]:
        """Get the dev-container's local parent images to build, lowest first, as (tag, dir)."""
        val, _ = self.__get(("dev-container", "parent-builds"))
        if not isinstance(val, list):
            return []
        ret: list[tuple[str, Path]] = []
        for entry in val:
            if not isinstance(entry, dict):
                raise self.__key_err(("dev-container", "parent-builds"), "expected {dir, tag} entries")
            tag = entry.get("tag")
            path = entry.get("dir")
            if not isinstance(tag, str) or not isinstance(path, str):
                raise self.__key_err(("dev-container", "parent-builds"), "expected {dir, tag} entries")
            ret.append((tag, Path(path)))
        return ret

    def project_str(self, *key: JsonPathPart) -> str | None:
        """Get the project key."""
        val = self.__opt_str("project", *key)
        if val is None:
            return None
        return str(val)

    def project_bridge_commands(self) -> list[str]:
        """Get the list of bridge commands."""
        cmd_list, _ = self.__get(("project", "commands"))
        if not isinstance(cmd_list, list):
            return []
        return [
            str(cmd["command"])
            for cmd in cmd_list
        ]

    def save(self, outfile: str | Path) -> None:
        """Save the current configuration to the given file."""
        with open(outfile, "w", encoding="utf-8") as out:
            json.dump(self._config, out)

    def dumps(self) -> str:
        """Output the current configuration to a string."""
        return json.dumps(self._config)

    def reprocess_with(
        self,
        project_config_file: str | None,
        project_dir: str | None,
        dev_container_workspace_dir: str | None,
        dev_container_settings_dir: str | None,
    ) -> None:
        """Reprocess this configuration using the new settings."""
        # Save the configuration for reprocessing.
        tfd, tmp = tempfile.mkstemp()
        os.close(tfd)
        self.save(tmp)

        # Reprocess, now with the project file.
        try:
            config = generate_config(
                bin_dir=self.bin_dir,
                distribution_dir=self.require_path("scoter", "distribution-dir"),
                project_config_file=project_config_file,
                project_dir=project_dir,
                dev_container_workspace_dir=dev_container_workspace_dir,
                dev_container_settings_dir=dev_container_settings_dir,
                user_setup=[],
                universal_file=tmp,
                no_setup_search=True,
            )
            self._config = config
        finally:
            os.unlink(tmp)

    @staticmethod
    def load_project_from_args(args: argparse.Namespace) -> "UserConfig":
        """Load the settings + the project from the CLI arguments.
        
        Uses the standard CLI arguments for the project settings, as
        configured through the '_add_project_args' and
        '_add_settings_args' function.
        """
        return UserConfig.load_project(
            bin_dir=args.bin_dir,
            distribution_dir=args.distribution_dir,
            user_setup=args.user_config,
            project_config_file=args.project_config,
            project_dir=args.project_dir,
        )

    @staticmethod
    def load_settings_from_args(args: argparse.Namespace) -> "UserConfig":
        """Load the settings from the CLI arguments.
        
        Uses the standard CLI arguments for the project settings, as
        configured through the '_add_settings_args' function.
        """
        return UserConfig.load_settings(
            bin_dir=args.bin_dir,
            distribution_dir=args.distribution_dir,
            user_setup=args.user_config,
        )

    @staticmethod
    def load_project(
        *,
        bin_dir: str | None,
        distribution_dir: str | None,
        user_setup: list[str],
        project_config_file: str | None,
        project_dir: str | None,
    ) -> "UserConfig":
        """Load the settings + the project information."""
        dist_dir = UserConfig.require_global_config_path(distribution_dir)
        resolved_bin_dir = UserConfig.find_bin_dir(bin_dir, dist_dir)
        settings = UserConfig(generate_config(
            bin_dir=resolved_bin_dir,
            distribution_dir=dist_dir,
            project_config_file=project_config_file,
            project_dir=project_dir,
            dev_container_workspace_dir=None,
            dev_container_settings_dir=None,
            user_setup=user_setup,
            universal_file=None,
            no_setup_search=False,
        ), resolved_bin_dir)

        # Ensure the project configuration was found.
        if not settings.optional_path("scoter", "project-config"):
            # It wasn't, so use the search path to find it.
            project_file = settings.search_path("scoter", "project-file-search", None, is_dir=False)
            if not project_file:
                raise ConfigErr(
                    "could not find the project file from this location.  "
                    "Try passing it through the --project argument."
                )
            # And reload the setup with that discovered path.
            settings.reprocess_with(
                project_config_file=str(project_file),
                project_dir=None,
                dev_container_workspace_dir=None,
                dev_container_settings_dir=None,
            )
        return settings

    @staticmethod
    def load_settings(
        *,
        bin_dir: str | None,
        distribution_dir: str | None,
        user_setup: list[str],
    ) -> "UserConfig":
        """Load just the settings."""
        dist_dir = UserConfig.require_global_config_path(distribution_dir)
        resolved_bin_dir = UserConfig.find_bin_dir(bin_dir, dist_dir)
        return UserConfig(generate_config(
            bin_dir=resolved_bin_dir,
            distribution_dir=dist_dir,
            project_config_file=None,
            project_dir=None,
            dev_container_workspace_dir=None,
            dev_container_settings_dir=None,
            user_setup=user_setup,
            universal_file=None,
            no_setup_search=False,
        ), resolved_bin_dir)

    @staticmethod
    def require_global_config_path(distribution_dir: str | None) -> Path:
        """Find where the OS installed the global configuration path.
        
        This can come from the user supplied argument.  That's common for a
        distribution to supply a `/usr/local/bin/scoter` shell script that runs:
        
        `/usr/local/share/scoter/scoter --distribution-dir=/usr/local/share/scoter "$@"`
        """
        if distribution_dir:
            # Use this value, even if it doesn't exist.
            if os.path.isdir(distribution_dir):
                # Don't use realpath here.
                return Path(os.path.abspath(distribution_dir))
            raise ConfigErr(f"The distribution directory {distribution_dir} doesn't exist.")

        # Try to find it based on the location of this script file.
        this = os.path.realpath(__file__)
        if os.path.isfile(this):
            this_dir = os.path.dirname(this)
            # All we care about here is that the executables exist.
            if (
                os.path.isdir(os.path.join(this_dir, DISTRIBUTION_BIN_DIR, SCOTER_HOST_EXEC))
                and os.path.isdir(os.path.join(this_dir, DISTRIBUTION_BIN_DIR, GEN_SCOTER_UC_EXEC))
            ):
                return Path(this_dir)

        # Couldn't find it.
        raise ConfigErr(
            "Could not find the Surf Scoter distribution directory.  "
            "You will need to pass it with the '--distribution-dir=PATH' argument."
        )

    @staticmethod
    def find_bin_dir(bin_dir: str | None, dist_dir: Path) -> Path:
        """Find the bin/ directory."""
        if bin_dir:
            if not os.path.isdir(bin_dir):
                raise ConfigErr(f"Could not find bin directory '{bin_dir}'")
            return Path(bin_dir)
        return dist_dir / DISTRIBUTION_BIN_DIR

    def __require_list(self, *path: JsonPathPart) -> ListJsonData:
        obj, exists = self.__get(path)
        if exists and isinstance(obj, list):
            return obj
        raise UserConfig.__key_err(path, "expected list")

    def __require_list_path(self, *path: JsonPathPart) -> list[tuple[list[JsonPathPart], JsonData]]:
        ret: list[tuple[list[JsonPathPart], JsonData]] = []
        idx = 0
        for item in self.__require_list(*path):
            ret.append(([*path, idx], item))
            idx += 1
        return ret

    def __opt_str(self, *path: JsonPathPart) -> str | None:
        obj, exists = self.__get(path)
        if not exists or obj is None:
            return None
        if isinstance(obj, str | int | float | bool):
            return str(obj)
        raise UserConfig.__key_err(path, "expected string")

    def __get(self, path: Sequence[JsonPathPart]) -> tuple[JsonData, bool]:
        cur: JsonData = self._config
        for part in path:
            if isinstance(cur, dict) and isinstance(part, str):
                if part in cur:
                    cur = cur[part]
                else:
                    return (None, False)
            elif isinstance(cur, list) and isinstance(part, int):
                if part >= 0 and part < len(cur):
                    cur = cur[part]
                else:
                    return (None, False)
            else:
                return (None, False)
        return (cur, True)

    @staticmethod
    def __key_err(path: Sequence[JsonPathPart], msg: str) -> ConfigErr:
        path_str = "/".join([str(p) for p in path])
        return ConfigErr(f"bad universal configuration file format at {path_str}: {msg}")


# ---------------------------------------------------------------------
# User SSH Setup Understanding.

@dataclass(frozen=True)
class SshConfigLine:
    """A single line from the ssh_config file.
    
    Every line is either before the first 'Host' entry, or has a host value.
    'Host' lines have their own Host as their host value.

    Use a negative value for the 'line_no' to indicate it's an addition.

    The 'line' value MUST terminate in an EOL character.  This means rewriting
    the file can cause an extra EOL that wasn't there originally.
    """
    line: str
    line_no: int
    valid: bool
    host: str | None
    key: str | None
    value: str | None

    @staticmethod
    def parse_line(host: str | None, line: str, line_no: int) -> "SshConfigLine":
        """Parse the single line of the ssh_config file."""
        if line_no <= 0:
            raise ValueError("line_no must be positive")
        if line[-1] not in "\r\n":
            # See note above.
            line += "\n"
        in_scope = line.split("#")[0].strip()
        if not in_scope:
            return SshConfigLine(
                line=line, line_no=line_no, valid=True, host=host, key=None, value=None
            )
        idx = 0
        for val in in_scope:
            # Because of the strip() at the start and the 'if not in_scope' line,
            # idx will always be > 0 here, which means the extracted key is
            # already stripped.
            if val.isspace():
                key = in_scope[:idx]
                value = in_scope[idx:].strip()  # Note: should just need an 'lstrip()' here.
                if value and key.lower() == "host":
                    # Switch the host to this one.
                    return SshConfigLine(
                        line=line, line_no=line_no, valid=True, host=value, key=key, value=value
                    )
                return SshConfigLine(
                    line=line, line_no=line_no, valid=bool(value), host=host, key=key, value=value
                )
            idx += 1
        return SshConfigLine(
            line=line, line_no=line_no, valid=False, host=host, key=None, value=None
        )


class SshConfig:
    """Handler for reading and managing the ssh config file."""

    def __init__(self, hosts: dict[str | None, list[SshConfigLine]]) -> None:
        self.by_host = hosts
        self._last_new_line = 0

    def format_errors(self) -> list[SshConfigLine]:
        """Get all lines with validation problems."""
        ret: list[SshConfigLine] = []
        for lines in self.by_host.values():
            for line in lines:
                if not line.valid:
                    ret.append(line)
        # This expects that only existing, parsed lines have issues.
        return sorted(ret, key=lambda x: x.line_no)

    def has_host(self, host: str) -> bool:
        """Test if the config file has the host block."""
        return host in self.by_host

    def add_key_value(self, host: str, key: str, value: str) -> None:
        """Add the key/value to the host block.
        
        The host block must exist.
        """
        if host not in self.by_host:
            raise ConfigErr(f"ssh config does not have Host block {host}")
        self._last_new_line -= 1
        self.by_host[host].append(SshConfigLine(
            line=f"  {key} {value}\n",
            line_no=self._last_new_line,
            valid=True,
            host=host,
            key=key,
            value=value,
        ))

    def add_comment(self, host: str | None, text: str) -> None:
        """Add a comment to the host (or at the file start).
        
        The host block must already exist.
        """
        if host not in self.by_host:
            raise ConfigErr(f"ssh config does not have Host block {host}")
        indent = "# "
        if host:
            indent = "  # "
        self._last_new_line -= 1
        self.by_host[host].append(SshConfigLine(
            line=indent + text + "\n",
            line_no=self._last_new_line,
            valid=True,
            host=host,
            key=None,
            value=None,
        ))

    def add_host(self, host: str) -> None:
        """Add a new host block to the config at the end."""
        if host in self.by_host:
            raise ConfigErr(f"ssh config already has Host block {host}")
        self._last_new_line -= 1
        self.by_host[host] = [
            SshConfigLine(
                line=f"Host {host}\n",
                line_no=self._last_new_line,
                valid=True,
                host=host,
                key="Host",
                value=host,
            )
        ]

    def get_host_key(self, host: str, key: str) -> str | None:
        """Read the host's key."""
        if host not in self.by_host:
            return None
        key = key.lower()  # keys are case insensitive
        for line in self.by_host[host]:
            if line.key and line.key.lower() == key:
                return line.value
        return None

    def output(self) -> str:
        """Convert the by-host collection into a Host list.
        
        This will correctly interweave the newly added lines into the configuration.
        """
        if not self.by_host:
            return ""
        max_lineno = max([max(n.line_no for n in h) for h in self.by_host.values()])

        def line_order(v: SshConfigLine) -> int:
            if v.line_no <= 0:
                # subtraction -> adding the two together.
                return max_lineno + 1 - v.line_no
            return v.line_no

        host_order = sorted(
            self.by_host.keys(),
            # The host line is always first in the host block.
            # In the case of the None host, it's always first in the file, if it exists.
            key=lambda a: line_order(self.by_host[a][0]),
        )
        ret = ""

        for host in host_order:
            lines = sorted(self.by_host[host], key=line_order)
            ret += "".join([n.line for n in lines])

        return ret

    @staticmethod
    def parse_file(contents: str) -> "SshConfig":
        """Parse a whole ssh_config file."""
        ret: dict[str | None, list[SshConfigLine]] = {}
        host: str | None = None
        line_no = 0
        for line in contents.splitlines(keepends=True):
            line_no += 1
            parsed = SshConfigLine.parse_line(host, line, line_no)
            host = parsed.host
            lines = ret.get(parsed.host)
            if not lines:
                lines = []
                ret[parsed.host] = lines
            lines.append(parsed)
        return SshConfig(ret)

    @staticmethod
    def read_ssh_config(config: UserConfig) -> "SshConfig | None":
        """Read the ssh config and return only if valid."""
        has_error = False
        private_key = config.require_path("ssh", "key-file")
        if not private_key.is_file():
            print(
                f"The ssh private key file ({private_key}) does not exist.  "
                f"You probably forgot to run the 'user-setup' sub-command.",
                file=sys.stderr,
            )
            has_error = True

        ssh_config = SshConfig({})
        ssh_config_file = config.require_path("ssh", "config-file")
        if not ssh_config_file.is_file():
            print(
                f"The ssh config file ({ssh_config_file}) does not exist.  "
                f"You probably forgot to run the 'user-setup' sub-command.",
                file=sys.stderr,
            )
            has_error = True
        else:
            with open(ssh_config_file, "r", encoding="utf-8") as inp:
                ssh_config = SshConfig.parse_file(inp.read())
            format_errs = ssh_config.format_errors()
            if format_errs:
                print(f"Thee ssh config file ({ssh_config_file}) has problems:", file=sys.stderr)
                for line in format_errs:
                    print(f"  {line.line_no}: {line.line.rstrip()}")
                has_error = True

        if has_error:
            return None
        return ssh_config


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
    suc: DictJsonData

    @staticmethod
    def from_json(file: Path, raw: JsonData) -> "EnvironmentDesc":
        """Extract this from JSON formatted data."""

        def as_dict(data: JsonData) -> DictJsonData:
            if not isinstance(data, dict):
                raise ConfigErr(
                    f"active environment file ({file}) expected dict, found {type(data)}"
                )
            return data

        def as_str(data: JsonData) -> str:
            if not isinstance(data, str):
                raise ConfigErr(
                    f"active environment file ({file}) expected str, found {type(data)}"
                )
            return data

        data = as_dict(raw)
        return EnvironmentDesc(
            host_prefix=as_str(data.get("host_prefix")),
            host_suffix=as_str(data.get("host_suffix")),
            dev_env_name=as_str(data.get("dev_env_name")),
            firewall_router_name=as_str(data.get("firewall_router_name")),
            network_gateway_name=as_str(data.get("network_gateway_name")),
            project_dir=as_str(data.get("project_dir")),
            resources_dir=as_str(data.get("resources_dir")),
            workspace_dir=as_str(data.get("workspace_dir")),
            setup_dir=as_str(data.get("setup_dir")),
            compose_file=as_str(data.get("compose_file")),
            compose_env_file=as_str(data.get("compose_env_file")),
            dev_env_ssh_port=as_str(data.get("dev_env_ssh_port")),
            dev_env_ssh_user=as_str(data.get("dev_env_ssh_user")),
            created_at=as_str(data.get("created_at")),
            suc=as_dict(data.get("suc")),
        )

    def to_json(self) -> JsonData:
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
                raise ConfigErr(
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

    def to_json(self) -> JsonData:
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
        raise ConfigErr("Too many registered environments.")

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
            raise ConfigErr(
                f"The active environment lock is in place ({lock_file}).  "
                f"That means either something crashed while working on an environment, "
                f"or you tried to start this environment while another is starting or stopping."
            )
        if not file.is_file():
            # No file yet.  Mark the lock and return empty.
            lock_file.write_text("[]", encoding="utf-8")
            _touch(lock_file, 0o400)
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
            _touch(lock_file, 0o600)  # ensure it exists and is writable.
            orig, environs = ActiveEnvironmentSet._read_contents(file)
            lock_file.write_text(orig, encoding="utf-8")
            _touch(lock_file, 0o400)  # make read-only
            return ActiveEnvironmentSet(
                file=file,
                lock_file=lock_file,
                orig=orig,
                environs=environs,
            )
        except OSError as err:
            # Could not capture the lock.
            raise ConfigErr(
                f"The active environment lock is in place ({lock_file}).  "
                f"That means either something crashed while working on an environment, "
                f"or you tried to start this environment while another is starting or stopping."
            ) from err

    @staticmethod
    def _read_contents(file: Path) -> tuple[str, list[EnvironmentDesc]]:
        if file.is_file():
            # TODO put a cap on file size.
            with open(file, "r", encoding="utf-8") as inp:
                text = inp.read()
            data: JsonData = json.loads(text)
            if not isinstance(data, list):
                raise ConfigErr(f"wrong formatted active environment set {file}")
            return (text, [EnvironmentDesc.from_json(file, e) for e in data])
        return ("", [])

    def close(self) -> None:
        """Close without saving, and disallow additional save actions."""
        # Note: this is explicitly not run at __del__() time.
        # This allows for leaving things in a reserved state for when something went sideways.
        if self._lock:
            if self._is_lock_owned():
                _touch(self._lock, 0o600)  # turn writable
                _rm(self._lock)
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
            _touch(self._lock, 0o600)  # turn writable
            _rm(self._lock)
            self._lock = None
        return True


# ---------------------------------------------------------------------
# Basic file operations

def _backup(file: Path, backup_ext: str) -> bool:
    """Backup the file.
    
    Returns 'True' if the backup was successful.
    """
    if not file.is_file():
        # The file to back up doesn't exist, so there's nothing to back up.
        # This is fine.
        return True
    to_file = Path(str(file) + backup_ext)
    if to_file.exists():
        _rm(to_file)
    # Refresh metadata in the check.
    if not os.path.exists(to_file):
        file.copy(to_file, preserve_metadata=True)
        return True
    return False


def _restore(file: Path, backup_ext: str, delete_allowed: bool = False) -> bool:
    """Restore the file from backup.
    
    Returns 'True' if the restore succeeded.

    If 'delete_allowed' is True, then the restore will remove the
    file if there exists no backup.

    If the file can't be removed, then the backup goes untouched.
    """
    from_file = Path(str(file) + backup_ext)
    if not from_file.exists():
        # There is no backup file.
        if delete_allowed:
            # Only delete the source file if the caller requests it.
            if not _rm(file):
                # It still exists.  Can't restore from backup.
                return False
        return True
    try:
        # TODO ensure 'move' is Python 3.12 compatible.
        from_file.move(file)
    except OSError:
        return False
    return True


def _touch(file: Path, mode: int) -> None:
    """Ensure the file exists with the given mode."""
    file.parent.mkdir(exist_ok=True, parents=True)
    file.touch(mode)
    os.chmod(file, mode)


def _rm(file: Path) -> bool:
    """Try to remove the file.  Does not raise an error, but reports it to stderr.
    
    If the file is a directory, it will try to remove it recursively.
    """
    if not file.exists():
        return True
    if file.is_mount():
        print(f"Warning: cannot delete mount point at {file}", file=sys.stderr)
        return False
    try:
        if (
            file.is_file()
            or file.is_symlink()
            or file.is_junction()
            or file.is_char_device()
            or file.is_socket()
        ):
            file.unlink()
        elif file.is_dir():
            shutil.rmtree(file)
        else:
            print(f"Warning: could not delete unsupported file type at {file}", file=sys.stderr)
            return False

        # Final check if things worked.
        if file.exists():
            print(f"Warning: could not delete {file}", file=sys.stderr)
            return False
        return True
    except PermissionError:
        print(f"Warning: permission issue deleting {file}", file=sys.stderr)
        return not file.exists()
    except IOError as err:
        print(f"Warning: {err}", file=sys.stderr)
        return not file.exists()


# ---------------------------------------------------------------------
# Generate the SUC file

def generate_config(
    *,
    bin_dir: Path | None,
    distribution_dir: Path,
    project_config_file: str | None,
    project_dir: str | None,
    dev_container_workspace_dir: str | None,
    dev_container_settings_dir: str | None,
    user_setup: list[str],
    universal_file: str | None,
    no_setup_search: bool,
) -> DictJsonData:
    """Run the gen-scoter-uc program."""
    if bin_dir:
        gen_uc = _require_exe(bin_dir / GEN_SCOTER_UC_EXEC)
    else:
        gen_uc = _require_exe(distribution_dir / DISTRIBUTION_BIN_DIR / GEN_SCOTER_UC_EXEC)
    args: list[str] = [str(gen_uc), "--distribution-dir", str(distribution_dir)]
    if project_config_file:
        args.extend(("--config", project_config_file))
    if project_dir:
        args.extend(("--project-dir", project_dir))
    if dev_container_workspace_dir:
        args.extend(("--dev-container-workspace-dir", dev_container_workspace_dir))
    if dev_container_settings_dir:
        args.extend(("--dev-container-settings-dir", dev_container_settings_dir))
    for user_setup_loc in user_setup:
        args.extend(("--user-setup", user_setup_loc))
    if universal_file:
        args.extend(("--universal", universal_file))
    if no_setup_search:
        args.append("--no-setup-search")
    res = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        # let stderr be passed directly to the user.
        check=False,
    )
    if res.returncode != 0:
        raise ConfigErr("Problem loading the configuration")

    try:
        ret: JsonData = json.loads(res.stdout)
    except Exception as err:
        raise ConfigErr(f"'{gen_uc}' generated an invalid JSON document: {err}") from err
    if isinstance(ret, dict):
        return ret
    raise ConfigErr(f"'{gen_uc}' generated an unexpected format.")


def _find_exe(name: str | Path) -> Path | None:
    """Find the executable."""
    # Which will work if you pass it the absolute location.
    loc = shutil.which(name)
    if loc:
        # If 'name' is relative, which will return a relative path.
        return Path(os.path.abspath(loc))
    if _is_windows():
        # Check for a missing PATHEXT.
        path_ext = os.environ.get("PATHEXT", "").split(os.path.pathsep)
        for ext in path_ext:
            if not ext:
                continue
            if str(name).endswith(ext) and os.path.isfile(name):
                return Path(os.path.abspath(name))
        for ext in path_ext:
            if not ext:
                continue
            if os.path.isfile(str(name) + ext):
                return Path(os.path.abspath(name))
        # Not a Windows executable.
        return None
    if os.path.isfile(name) and os.access(name, os.X_OK):
        return Path(os.path.abspath(name))
    # Not a non-Windows executable.
    return None


def _require_exe(name: str | Path) -> Path:
    """Require that the executable exists."""
    ret = _find_exe(name)
    if not ret:
        raise ConfigErr(f"could not find '{name}'")
    return ret


def _is_windows() -> bool:
    return platform.system() == "Windows"


# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# Sub Command Handlers
# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# Each sub-command handler has the signature (argparse.Namespace) -> int
# They also register an argument parser to the 'SUB_COMMAND_ARGUMENTS' value.
SUB_COMMAND_ARGUMENTS: dict[str, Callable[[argparse.ArgumentParser], None]] = {}


# ---------------------------------------------------------------------
# Start and manage a dev environment.

def run_env_parser(parser: argparse.ArgumentParser) -> None:
    """Configure the CLI arguments."""
    parser.description = (
        "Start and monitor the project dev container.  Press 'ctrl-c' while it's "
        "running to terminate and clean up the environment."
    )
    parser.set_defaults(func=run_env_runner)
    _add_project_args(parser)
    parser.add_argument(
        "--clean-workspace",
        action="store_true",
        dest="clean_workspace",
        default=False,
        help=(
            "If the dev-env workspace directory already exists from a previous run, "
            "remove it and recreate it."
        ),
    )
    parser.add_argument(
        "--no-workspace-fetch",
        action="store_false",
        dest="workspace_fetch",
        default=True,
        help=(
            "If the workspace directory exists, then don't fetch updates into it "
            "from the project directory.  This can lead to the workspace directory "
            "becoming out-of-date.  If the workspace directory doesn't exist, it "
            "will still be populated from the project (essentially ignoring this "
            "flag)."
        ),
    )
    parser.add_argument(
        "--vs-settings",
        action="store",
        dest="vs_settings",
        help="Update your VS user settings file to run in this container.",
    )


SUB_COMMAND_ARGUMENTS["run"] = run_env_parser


def run_env_runner(args: argparse.Namespace) -> int:
    """Start the dev environment."""
    config = UserConfig.load_project_from_args(args)
    vcs_section = config.project_str("vcs")
    if vcs_section:
        vcs_section += VCS_SECTION_SUFFIX

    # Prepare the active environment set for writing.
    # Note that this requires explicit "close" mechanics when successfully used.
    active_envs = ActiveEnvironmentSet.open(
        config.require_path("dev-env", "running-env-config"),
        config.require_path("dev-env", "running-env-config-lock"),
    )
    try:
        # -- Discovery
        # Configure the sshd port.  It might be dynamic.
        _configure_sshd_port(config, active_envs)

        # Setup the SSH config file for running.
        host_prefix = _setup_ssh_config(config)
        if not host_prefix:
            active_envs.close()
            return 1

        # Discover the host suffix to use.
        host_suffix = active_envs.reserve_environ(host_prefix)
        host = host_prefix + host_suffix

        # -- Build the containers
        image_tags = _build_containers(config, host) # (dev-env, firewall, gateway)
        if not image_tags:
            active_envs.close()
            return 1

        # Remember to mark the work directories as ignored.

        # -- workspace directory
        # Setup the workspace directory via the vcs command.
        workspace_dir = _setup_workspace(
            config, vcs_section, args.clean_workspace, args.workspace_fetch
        )
        _ignore_directory(config, vcs_section, workspace_dir)

        # -- settings directory
        setup_dir = config.active_location("dev-container-settings-dir")
        if setup_dir is None:
            setup_dir = config.require_path("workspace", "setup-dir")
            config.set_active_location("dev-container-settings-dir", setup_dir)
        setup_dir.mkdir(exist_ok=True)
        _ignore_directory(config, vcs_section, setup_dir)

        # Write the authorized keys file.
        key_file = config.require_path("ssh", "key-file")
        pub_key_file = Path(str(key_file) + PUB_KEY_EXT)
        # Ensure they exists.
        if not key_file.is_file() or not pub_key_file.is_file():
            raise ConfigErr(
                f"Your ssh key pair ({key_file} and {pub_key_file}) don't exist.  "
                f"Make sure you ran user setup first."
            )
        pub_key_file.copy(setup_dir / AUTHORIZED_KEYS_FILE_NAME)
        _touch(setup_dir / AUTHORIZED_KEYS_FILE_NAME, 0o666)

        # Create the host key.
        host_key_file = setup_dir / CONTAINER_HOST_KEY_FILE_NAME
        if not host_key_file.exists():
            print("Creating host key...")
            config.run_command(
                "ssh", "mk-host-key", capture_stdout=True,
                template={"host-key-file": str(host_key_file)},
            )
        _touch(host_key_file, 0o666)
        # Add that as a known host
        # The file should already exist from the user setup.
        _add_known_host(config, host, host_key_file)

        allowed_domains_file = config.require_path("dev-env", "allowed-domains")
        if allowed_domains_file.exists():
            allowed_domains_file.copy(setup_dir / ALLOWED_DOMAINS_FILE_NAME)
        else:
            _touch(setup_dir / ALLOWED_DOMAINS_FILE_NAME, 0o666)

        # Write the host-bridge command list into the setup directory.
        with open(setup_dir / BRIDGE_COMMANDS_FILE_NAME, "w", encoding="utf-8") as inp:
            inp.write("\n".join(config.project_bridge_commands()))

        # -- resources directory
        resources_dir = config.require_path("workspace", "resources-dir")
        resources_dir.mkdir(exist_ok=True)
        _ignore_directory(config, vcs_section, resources_dir)

        # Setup the .env file in the resources directory.
        _write_docker_compose_env_file(
            config=config, file=resources_dir / COMPOSE_ENV_FILENAME,
            host=host, workspace_dir=workspace_dir,
            settings_dir=setup_dir,
        )

        # Setup the docker-compose file in the resources directory.
        config.require_path("dev-env", "docker-compose").copy(
            resources_dir / DOCKER_COMPOSE_FILENAME
        )

        # Save off a copy of the config file
        universal_config_file = resources_dir / "suc.json"
        config.save(universal_config_file)
    except:
        print("Aborting execution.", file=sys.stderr)
        # Close without saving.
        active_envs.close()
        raise

    # -- Record this environment
    # If the save fails, then something grabbed the lock
    # from under us.
    if not active_envs.save(close=False):
        raise ConfigErr(
            "Lost our lock on the active environments.  "
            "Please wait longer before launching a second one."
        )
    active_envs.update_environ(EnvironmentDesc(
        host_prefix=host_prefix,
        host_suffix=host_suffix,

        # See _write_docker_compose_env_file
        dev_env_name=host,
        firewall_router_name=f"{host}-fr",
        network_gateway_name=f"{host}-ng",
        
        project_dir=str(config.require_active_location("project-dir")),
        resources_dir=str(resources_dir),
        workspace_dir=str(workspace_dir),
        setup_dir=str(setup_dir),
        compose_file=str(resources_dir / DOCKER_COMPOSE_FILENAME),
        compose_env_file=str(resources_dir / COMPOSE_ENV_FILENAME),
        dev_env_ssh_port=str(config.dev_container_sshd_port()),
        dev_env_ssh_user=config.dev_container_str("user"),
        created_at=datetime.datetime.now().isoformat(),
        suc=json.loads(config.dumps()),  # expensive deep copy
    ))
    active_envs.save(close=True)

    # -- start the containers
    # Start the docker-compose.yaml (daemon mode).
    print("Starting the environment cluster...")
    config.run_command(
        "docker", "compose-up",
        template={
            "env-file": str(resources_dir / COMPOSE_ENV_FILENAME),
            "compose-file": str(resources_dir / DOCKER_COMPOSE_FILENAME),
        },
        capture_stdout=False,
        capture_stderr=False,
    )

    try:
        # Tell the user the setup for the container (host name).
        print("==========================================")
        print(f"dev-container running as {host}")
        print("Waiting for it to start...")
        if not _wait_for_container(config, host, timeout_sec=CONTAINER_START_TIME_SEC):
            print("Container didn't start in time.")
        else:
            # Start scoter-host
            if args.bin_dir:
                scoter_host = _require_exe(Path(args.bin_dir) / SCOTER_HOST_EXEC)
            else:
                scoter_host = _require_exe(
                    Path(args.distribution_dir) / DISTRIBUTION_BIN_DIR / SCOTER_HOST_EXEC
                )
            print([scoter_host, "--universal", str(universal_config_file), host])
            res = subprocess.run(
                [scoter_host, "--universal", str(universal_config_file), host],
                check=False,
            )
            if res.returncode != 0:
                print("Failed running the host process.")

    except KeyboardInterrupt:
        print("User requested termination.")

    # -- Tear it all down.

    # Shut down the containers
    print("Stopping the environment cluster...")
    config.run_command(
        "docker", "compose-down",
        template={
            "env-file": str(resources_dir / COMPOSE_ENV_FILENAME),
            "compose-file": str(resources_dir / DOCKER_COMPOSE_FILENAME),
        },
    )

    # Remove from active mode.
    active_envs = ActiveEnvironmentSet.open(
        config.require_path("dev-env", "running-env-config"),
        config.require_path("dev-env", "running-env-config-lock"),
    )
    active_envs.rm_environ_by_host(host)
    active_envs.save(close=True)

    # Clean up the setup and resource directories.
    #_rm(setup_dir)
    #_rm(resources_dir)
    # Keep the 'workspace' directory, though.

    return 0


def _build_containers(config: UserConfig, host: str) -> tuple[str, str, str] | None:
    # Build the dev-env container + firewall-router + network-gateway.
    ret = (
        config.require_value("dev-env", "dev-env-image-prefix") + host,
        config.require_value("dev-env", "firewall-router-image"),
        config.require_value("dev-env", "network-gateway-image"),
    )
    # The dev-env's local parents come first, lowest first.
    containers: list[tuple[str, str, Path | None]] = [
        ("dev-env parent", tag, path)
        for tag, path in config.dev_container_parent_builds()
    ]
    containers += [
        (
            "dev-env",
            ret[0],
            config.active_location("dev-container-path"),
        ),
        (
            "firewall-router",
            ret[1],
            config.require_path("dev-env", "firewall-router"),
        ),
        (
            "network-gateway",
            ret[2],
            config.require_path("dev-env", "network-gateway"),
        ),
    ]
    for kind,tag,path in containers:
        if not path:
            print(f"No build directory set for {kind} container", file=sys.stderr)
            return None
        print(f"Building {kind} image...")
        print(f"DEBUG {path}")
        res = config.run_command(
            "docker", "build",
            check=False,
            capture_stderr=True,
            template={
                "tag": tag,
                "dir": str(path),
            },
        )
        # TODO write the stderr + stdout to a file in the resources directory
        # so the user can inspect what happened.
        if res.returncode != 0:
            print(res.stdout.decode("utf-8"))
            print(res.stderr.decode("utf-8"), file=sys.stderr)
            print(f"Failed building {kind} image ({path})", file=sys.stderr)
            return None
    return ret


def _wait_for_container(config: UserConfig, host: str, timeout_sec: float) -> bool:
    # Wait for the host to start running.
    end_at = time.time() + timeout_sec
    while time.time() < end_at:
        # Sleep at the start, because this should be called while the
        # containers are starting.
        time.sleep(1.0)
        res = config.run_command(
            "docker", "inspect-container",
            capture_stdout=True,
            capture_stderr=True,
            template={"name": host},
        )
        if res.returncode == 125:
            # Not found.
            continue
        elif res.returncode != 0:
            raise subprocess.CalledProcessError(
                returncode=res.returncode,
                cmd=res.args,
                output=res.stdout,
                stderr=res.stderr,
            )

        try:
            data: JsonData = json.loads(res.stdout)
            if isinstance(data, list):
                if len(data) > 0:
                    data = data[0]
            if not isinstance(data, dict):
                raise RuntimeError(f"unexpected JSON result: {res.stdout.decode('utf-8')}")
            state = data.get("State")
            print(f"host image state: {state}")
            if not isinstance(state, dict):
                # Assume not good state.
                continue
            if state.get("Running", False):
                return True
            if state.get("Status") == "exited":
                print(f"Host process exited quickly (exit code {state.get('ExitCode')})")
                print("=========== Log Start")
                config.run_command(
                    "docker", "image-logs",
                    capture_stdout=False,
                    capture_stderr=False,
                    check=False,
                    template={"name": host},
                )
                print("=========== Log End")
                return False
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    return False


def _setup_workspace(
    config: UserConfig,
    vcs_section: str | None,
    clean_workspace: bool,
    workspace_fetch: bool
) -> Path:
    # Setup the workspace directory.
    workspace_dir = config.active_location("dev-container-workspace-dir")
    if workspace_dir is None:
        workspace_dir = config.require_path("workspace", "workspace-dir")
        config.set_active_location("dev-container-workspace-dir", workspace_dir)
    if workspace_dir.exists():
        if clean_workspace:
            print(f"Cleaning out the workspace directory ({workspace_dir})")
            _rm(workspace_dir)
        elif workspace_fetch and vcs_section:
            print(f"Fetching your project into {workspace_dir} ...")
            config.run_command(
                vcs_section,
                "fetch-changes",
                cwd=workspace_dir,
            )
        else:
            print("Not copying project into the workspace.")
    if not workspace_dir.exists():
        if vcs_section:
            print("Cloning into the workspace")
            print(f"Cloning your project into {workspace_dir} ...")
            config.run_command(
                vcs_section,
                "local-clone",
                cwd=config.active_location("project-dir"),
                template={
                    "workspace-dir": str(workspace_dir),
                }
            )
        else:
            print("Not copying project into the workspace (no version control system defined).")
            workspace_dir.mkdir(parents=True)
    return workspace_dir


def _ignore_directory(config: UserConfig, vcs_section: str | None, path: Path) -> None:
    """Mark the directory as ignored by adding the name to the parent's ignore file."""
    if not vcs_section:
        # No ignore possible.
        return
    ignore_file_name = config.require_value(vcs_section, "ignore-file")
    if not ignore_file_name:
        # No ignore possible.
        return
    
    path.parent.mkdir(parents=True, exist_ok=True)
    ignore_file = path.parent / ignore_file_name
    ignore_lines: list[str]
    if ignore_file.exists():
        # load up the lines.
        with open(ignore_file, "r", encoding="utf-8") as out:
            # This includes newlines at the end.
            ignore_lines = out.readlines()
    else:
        ignore_lines = []

    path_pattern = f"/{path.name}/\n"
    if path_pattern not in ignore_lines:
        ignore_lines.append("\n")
        ignore_lines.append("# Surf Scoter work directory\n")
        ignore_lines.append(path_pattern)

        # Need to modify the file
        ignore_file.write_text("".join(ignore_lines), encoding="utf-8")


def _configure_sshd_port(config: UserConfig, envs: ActiveEnvironmentSet) -> None:
    port = config.dev_container_sshd_port()
    if port is not None:
        # Already set.
        return

    # Check the ssh config for all ports already declared,
    # and pick one that isn't active but has the same username.
    user = config.dev_container_str("user")
    all_config_ports: set[str] = set()
    available_config_ports: set[str] = set()
    ssh_config = SshConfig.read_ssh_config(config)
    if ssh_config:
        # It exists.  Find a port with the same username.
        # The port is baked into the host pattern expression.
        user_port_pattern = re.compile(
            r"^" +
            re.escape(config.require_value("ssh", "host-prefix")) +
            r"-(.*?)-" +
            r"-(\d+)-\*$"
        )
        for host in ssh_config.by_host.keys():
            if not host:
                continue
            match = user_port_pattern.match(host)
            if match:
                all_config_ports.add(match.group(2))
                if ssh_config.get_host_key(host, "user") == user:
                    # We have a winner.
                    available_config_ports.add(match.group(1))
    # Eliminate active environments' port.
    for env in envs.environs.values():
        if (
            env.dev_env_ssh_port in available_config_ports
            and env.dev_env_ssh_user == user
        ):
            available_config_ports.remove(env.dev_env_ssh_port)
    ssh_port = ""
    if available_config_ports:
        # Reuse one.
        ssh_port = tuple(available_config_ports)[0]
    else:
        # get an available one
        start_sshd_port = config.require_value("dev-env", "start-sshd-port")
        try:
            ssh_port_i = int(start_sshd_port)
        except (ValueError, TypeError) as err:
            raise ConfigErr(
                f"invalid dev-env values start-sshd-port ({start_sshd_port}): "
                f"it must be an integer"
            ) from err
        while ssh_port_i < TOP_PORT_NUMBER:
            ssh_port = str(ssh_port_i)
            if ssh_port not in all_config_ports:
                break
            ssh_port_i += 1
    if not ssh_port:
        raise ConfigErr("no sshd listening port available")
    config.set_dev_container_sshd_port(ssh_port)


def _add_known_host(config: UserConfig, host: str, host_key_file: Path) -> None:
    # Add the host to the known-hosts file.
    # It should already exist from the user setup command.
    known_hosts_file = config.require_path("ssh", "known-hosts")
    print(f"Removing the {host} entry from {known_hosts_file}")
    config.run_command(
        "ssh", "remove-known-host",
        template={"host": host},
        # If the host doesn't exist, that's fine.
        capture_stdout=True,
        capture_stderr=True,
    )
    pub_key_file = Path(str(host_key_file) + PUB_KEY_EXT)
    pub_key = pub_key_file.read_text(encoding="utf-8")
    # Format is: type + ' ' + key + ' ' + optional comment
    parts = pub_key.split(" ")
    if len(parts) < 2:
        raise ConfigErr(f"Bad pub key file format ({pub_key_file})")
    # Added into the known-hosts file in the format:
    #   host + ' ' + type + ' ' + key
    known_hosts = known_hosts_file.read_text(encoding="utf-8")
    if known_hosts and known_hosts[-1] != "\n":
        known_hosts += "\n"
    known_hosts += f"{host} {parts[0]} {parts[1]}\n"
    known_hosts_file.write_text(known_hosts, encoding="utf-8")


def _setup_ssh_config(config: UserConfig) -> str | None:
    # Ensure the right SSH config host block exists.
    # It generates the host prefix.
    # This isn't 100% right, in that it doesn't check for all host matches.
    ssh_config = SshConfig.read_ssh_config(config)
    if not ssh_config:
        return None

    user = config.dev_container_str("user")
    port = config.dev_container_str("sshd-port")  # force a value here.
    key_file = config.require_path("ssh", "key-file")
    proxy = config.require_run("ssh", "proxy-command")
    host_prefix = (
        config.require_value("ssh", "host-prefix") +
        user + "-" +
        port + "-"
    )
    host = host_prefix + "*"

    if ssh_config.has_host(host):
        errs: list[str] = []
        r_user = ssh_config.get_host_key(host, "user")
        if r_user != user:
            errs.append(f"  User {r_user}, requires {user}")
        r_identity = ssh_config.get_host_key(host, "IdentityFile")
        if not r_identity:
            errs.append("  No IdentityFile line")
        else:
            r_identity = os.path.expanduser(r_identity)
            if not os.path.samefile(r_identity, key_file):
                errs.append(f"  IdentityFile {r_identity}, requires {key_file}")
        r_proxy_raw = ssh_config.get_host_key(host, "ProxyCommand")
        if not r_proxy_raw:
            errs.append(f"  ProxyCommand requires {proxy}")
        else:
            r_proxy = shlex.split(r_proxy_raw)
            if r_proxy != proxy:
                errs.append(f"  ProxyCommand '{r_proxy_raw}', requires {proxy}")
        if errs:
            print(f"Your {ssh_config} file has issues that prevents execution:", file=sys.stderr)
            for err in errs:
                print(err, file=sys.stderr)
            return None
        # It's all good.
    else:
        ssh_config.add_host(host)
        ssh_config.add_comment(host, f"For Surf Scoter dev container user {user} on port {port}")
        ssh_config.add_key_value(host, "User", user)
        ssh_config.add_key_value(host, "IdentityFile", str(key_file))
        ssh_config.add_key_value(host, "IdentitiesOnly", "yes")
        ssh_config.add_key_value(host, "StrictHostKeyChecking", "yes")
        ssh_config.add_key_value(host, "ProxyCommand", shlex.join(proxy))
        ssh_config_file = config.require_path("ssh", "config-file")
        _backup(ssh_config_file, config.require_value("scoter", "backup-file-extension"))
        ssh_config_file.write_text(ssh_config.output(), encoding="utf-8")
        # Ensure the file mode wasn't changed.
        _touch(ssh_config_file, 0o600)

    return host_prefix


def _write_docker_compose_env_file(
    *,
    config: UserConfig, file: Path,
    host: str, workspace_dir: Path, settings_dir: Path,
) -> None:
    # Write all the settings to the docker-compose file.
    # 90% of this have well-defined meanings that all docker-compose must use.
    mounts = config.dev_container_mounts()
    if "workspace" not in mounts or "setup" not in mounts:
        raise ConfigErr(f"dev-env does not have expected mounts; has {mounts}")

    lines: dict[str, str] = {
        # Image tags
        #   from _build_containers above
        "FIREWALL_ROUTER": config.require_value("dev-env", "firewall-router-image"),
        "NETWORK_GATEWAY": config.require_value("dev-env", "network-gateway-image"),
        "DEV_ENV": config.require_value("dev-env", "dev-env-image-prefix") + host,

        # Container name
        "FIREWALL_ROUTER_NAME": f"{host}-fr",
        "NETWORK_GATEWAY_NAME": f"{host}-ng",
        "CONTAINER_NAME": host,

        # File paths
        "WORKSPACE_HOST": str(workspace_dir),
        "SETTINGS_HOST": str(settings_dir),

        # Mount points
        "WORKSPACE_CONTAINER": mounts["workspace"],
        "SETTINGS_CONTAINER": mounts["setup"],

        # Limits
        "PIDS_LIMIT": config.require_value("dev-env", "pids-limit"),
        "MEM_LIMIT": config.require_value("dev-env", "mem-limit"),
        "CPUS": config.require_value("dev-env", "cpus"),
        "LOGGING_MAX_SIZE": config.require_value("dev-env", "logging-max-size"),

        # Environment variables
        "SSHD_PORT_ENV": (config.dev_container_sshd_port_env() or "SSHD_PORT_ENV_UNSET"),
        "SSHD_PORT": (config.dev_container_sshd_port() or "not set"),
    }
    file.write_text("\n".join([f"{k}={v}" for k,v in lines.items()]), encoding="utf-8")


# ---------------------------------------------------------------------
# Stop a dev environment.

def stop_env_parser(parser: argparse.ArgumentParser) -> None:
    """Configure the CLI arguments."""
    parser.description = (
        "Stop the project dev container.  The 'run' should stop the configurations"
        "when it terminates, but unexpected issues can leave dangling resources.  "
        "Use this to stop run-away configurations."
    )
    parser.set_defaults(func=stop_env_runner)
    _add_project_args(parser)
    parser.add_argument(
        "--host",
        action="store",
        dest="host",
        help="Force a specific host to stop.  Doesn't need the project.",
    )
    parser.add_argument(
        "-f",
        "--fetch",
        action="store",
        dest="fetch_branch",
        help=(
            "Fetch the files from the workspace into your project directory into this branch."
        ),
    )
    parser.add_argument(
        "--force-clean",
        action="store_true",
        default=False,
        dest="force_clean",
        help=(
            "Force delete of the workspace files.  Setting this can potentially"
            "delete changes, so you should use this with the '--fetch' argument."
        ),
    )


SUB_COMMAND_ARGUMENTS["stop"] = stop_env_parser


def stop_env_runner(args: argparse.Namespace) -> int:
    """Stop the dev environment."""
    host = args.host
    if host:
        config = UserConfig.load_settings_from_args(args)
    else:
        config = UserConfig.load_project_from_args(args)
        # FIXME find the host by reviewing the 

    # FIXME
    assert config is not None

    return 0


# ---------------------------------------------------------------------
# Fetch changes from a dev environment.

def fetch_changes_parser(parser: argparse.ArgumentParser) -> None:
    """Configure the CLI arguments."""
    parser.description = "Fetch changes committed by the dev container to its local workspace."
    parser.set_defaults(func=fetch_changes_runner)
    _add_project_args(parser)
    parser.add_argument(
        "-b",
        "--branch",
        action="store",
        default="workspace-fetch",
        dest="branch",
        help=(
            "The branch name to fetch the workspace changes into."
        ),
    )

# TODO additional commands to use with this:
#      review, merge.
SUB_COMMAND_ARGUMENTS["fetch"] = fetch_changes_parser


def fetch_changes_runner(args: argparse.Namespace) -> int:
    """Fetch in changes from the dev environment."""
    config = UserConfig.load_project_from_args(args)

    # FIXME
    assert config is not None

    return 0


# ---------------------------------------------------------------------
# User-space setup.

def user_space_setup_parser(parser: argparse.ArgumentParser) -> None:
    """Configure the CLI arguments."""
    parser.description = "Configure your home directory for use with Surf Scoter."
    parser.set_defaults(func=user_space_setup_runner)
    parser.add_argument(
        "--non-interactive",
        action="store_false",
        default=True,
        dest="interactive",
        help=(
            "Configure your HOME directory automatically.  "
            "This will touch sensitive files, so use with care."
        ),
    )


SUB_COMMAND_ARGUMENTS["user-setup"] = user_space_setup_parser


def user_space_setup_runner(args: argparse.Namespace) -> int:
    """Run the user-space setup.
    
    This performs:
        1. key file construction, if it doesn't exist.
        2. Ensure the ssh config file exists.
    
    This doesn't construct the Host entry in the config file, because
    that can vary from container to container.
    """
    config = UserConfig.load_settings_from_args(args)
    interactive = bool(args.interactive)
    _gen_key_pair(config, interactive)
    _ensure_ssh_config(config, interactive)
    _ensure_ssh_known_hosts(config, interactive)
    _try_user_home(config, interactive)
    return 0


def _gen_key_pair(config: UserConfig, interactive: bool) -> None:
    # Step 1: generate the key pair.
    key_file = config.require_path("ssh", "key-file")
    pub_key_file = Path(str(key_file) + PUB_KEY_EXT)
    if key_file.is_file() and pub_key_file.is_file():
        answer = _read_user_input(
            f"The key file pair ({key_file}) already exists.  Replace? [yN]",
            interactive,
            "n",
        )
        if answer and answer[0] == "y":
            _rm(key_file)
            _rm(pub_key_file)
    elif key_file.exists() or pub_key_file.exists():
        answer = _read_user_input(
            f"Unfortunately, the key file pair used for allowing SSH "
            f"access into the dev environment "
            f"don't look right ('{key_file}' and '{pub_key_file}').  "
            f"Do you want to recreate them? [yN]",
            interactive,
            "n",
        )
        if answer and answer[0] == "y":
            _rm(key_file)
            _rm(pub_key_file)
    if key_file.exists() or pub_key_file.exists():
        print(f"The key file pair ('{key_file}' and '{pub_key_file}') exist; skipping creation.")
    else:
        # Creating a key pair when they don't already exist is safe.
        answer = _read_user_input(
            f"Do you want me to create the key pair ('{key_file}' and '{pub_key_file}')? [Yn]",
            interactive,
            "y",
        )
        if answer and answer[0] == "y":
            print("Generating the key pair.")
            config.run_command("ssh", "mk-key", capture_stdout=False)
        else:
            print(
                f"The ssh key pair ('{key_file}' and '{pub_key_file}') "
                f"don't exist, and the dev container won't work until they do."
            )


def _ensure_ssh_config(config: UserConfig, interactive: bool) -> None:
    # Step 2: Ensure the ssh config file exists.
    ssh_config_file = config.require_path("ssh", "config-file")
    if ssh_config_file.is_file():
        print(f"Good.  You already have the ssh config file ({ssh_config_file})")
    elif ssh_config_file.exists():
        answer = _read_user_input(
            f"Your ssh config ({ssh_config_file}) exists but isn't a file.  "
            f"Do you want me to remove what's there? [yN]",
            interactive,
            "n",
        )
        if answer and answer[0] == "y":
            shutil.rmtree(ssh_config_file, ignore_errors=True)

    if ssh_config_file.is_file() and not _is_windows():
        # Check its permissions.
        mode = os.stat(ssh_config_file).st_mode
        if mode & 0o0077 != 0:
            answer = _read_user_input(
                f"Your ssh config file ({ssh_config_file}) exists (excellent!), but has "
                f"wrong permissions ({mode:o}). Do you want me to fix that? [yN]",
                interactive,
                "n",
            )
            if answer and answer[0] == "y":
                os.chmod(ssh_config_file, 0o600)
                mode = os.stat(ssh_config_file).st_mode
                print(f"Changed the file ({ssh_config_file}) permissions to {mode:o}")
        else:
            print("... and that config file has the right permissions.")
    if not ssh_config_file.is_file():
        # Unlike the others, creating the file is a safe option.
        answer = _read_user_input(
            f"Your ssh config ({ssh_config_file}) doesn't exist.  "
            f"You'll need this to run Surf Scoter.  "
            f"Do you want me to create a blank one? [Yn]",
            interactive,
            "n",
        )
        if answer and answer[0] == "y":
            _touch(ssh_config_file, 0o600)
            print(f"Created an empty ssh config file ({ssh_config_file})")


def _ensure_ssh_known_hosts(config: UserConfig, interactive: bool) -> None:
    # Step 3: Ensure the known-hosts file exists.
    known_hosts_file = config.require_path("ssh", "known-hosts")
    if known_hosts_file.exists() and not known_hosts_file.is_file():
        answer = _read_user_input(
            f"Your ssh known-hosts file ({known_hosts_file}) exists but isn't a file.  "
            f"Do you want me to remove what's there? [yN]",
            interactive,
            "n"
        )
        if answer and answer[0] == "y":
            print(f"Removing whatever lives at {known_hosts_file}")
            if not _rm(known_hosts_file):
                print("Couldn't remove it.")
                return
    if not known_hosts_file.is_file():
        # An empty known-hosts file is fine.
        answer = _read_user_input(
            f"Your ssh known-hosts file ({known_hosts_file}) doesn't exist.  "
            f"Do you want me to create it for you? [Yn]",
            interactive,
            "y",
        )
        if answer and answer == "y":
            _touch(known_hosts_file, 0o600)
    elif not _is_windows():
        # Set the permissions without asking.
        _touch(known_hosts_file, 0o600)


def _try_user_home(config: UserConfig, interactive: bool) -> None:
    # Step 4: Create the ~/.config/surf-scoter.d directory + basic setup
    #  if the user-dir isn't set.
    user_dir = config.optional_path("scoter", "user-dir")
    if not user_dir:
        answer = _read_user_input(
            "The way you've invoked this command meant that I couldn't find "
            "a place to hold your personal configuration settings.  "
            "You'll need this directory in order to get running.  Do you want "
            "me to create it under ~/.config/surf-scoter.d (the default location)? [yN]",
            interactive,
            "n",
        )
        if answer and answer[0] == "y":
            user_dir = Path("~/.config/surf-scoter.d").expanduser()
            user_dir.mkdir(parents=True, exist_ok=True)
    user_setup = config.optional_path("scoter", "user-setup-file")
    if user_dir and not user_setup:
        user_setup = user_dir / "setup.yaml"
    if user_dir and user_setup and not user_setup.exists():
        answer = _read_user_input(
            f"You don't have a setup file in your user directory ({user_dir}).  "
            f"Would you like me to create a stub out there? [yN]",
            interactive,
            "n",
        )
        if answer and answer[0] == "y":
            # This could eventually copy a sample from the distribution defaults directory.
            _touch(user_setup, 0o666)


# ---------------------------------------------------------------------
# Clean up what Surf Scoter added to the user files.

def cleanup_user_parser(parser: argparse.ArgumentParser) -> None:
    """Configure the CLI arguments."""
    parser.description = (
        "Cleanup the files that this added or modified for the dev "
        "environments from your user directories."
    )
    parser.set_defaults(func=cleanup_user_runner)


SUB_COMMAND_ARGUMENTS["user-cleanup"] = cleanup_user_parser


def cleanup_user_runner(args: argparse.Namespace) -> int:
    """Stop the dev environment."""
    config = UserConfig.load_settings_from_args(args)

    # FIXME
    assert config is not None

    return 0


# ---------------------------------------------------------------------
# Configuration debugging.

def configuration_debug_parser(parser: argparse.ArgumentParser) -> None:
    """Configure the CLI arguments."""
    parser.description = "Output the Surf Scoter universal configuration file."
    parser.set_defaults(func=configuration_debug_runner)
    _add_project_args(parser)
    parser.add_argument(
        "-n",
        "--no-project",
        action="store_false",
        default=True,
        dest="use_project",
        help="Do not try to lookup the project files.",
    )

SUB_COMMAND_ARGUMENTS["parse-config"] = configuration_debug_parser


def configuration_debug_runner(args: argparse.Namespace) -> int:
    """Output the SUC to stdout."""
    if args.use_project:
        config = UserConfig.load_project_from_args(args)
    else:
        config = UserConfig.load_settings_from_args(args)
    print(config.dumps())
    return 0


# ---------------------------------------------------------------------
# Command and Argument helpers

def _add_project_args(parser: argparse.ArgumentParser) -> None:
    """Add the arguments used for handling a project."""
    parser.add_argument(
        "-c",
        "--project-config",
        action="store",
        dest="project_config",
        help="Set the project configuration file.",
    )
    parser.add_argument(
        "-d",
        "--project-dir",
        action="store",
        dest="project_dir",
        help="Set the project configuration directory.",
    )


def _add_settings_args(parser: argparse.ArgumentParser) -> None:
    """Add the arguments used for handling the user + global settings."""
    parser.add_argument(
        "-U",
        "--user-config",
        action="append",
        dest="user_config",
        default=[],
        help=(
            "Set your user configuration directory or file.  "
            "Also set through the SURF_SCOTER_HOME environment variable."
        ),
    )
    parser.add_argument(
        "-D",
        "--distribution-dir",
        action="store",
        dest="distribution_dir",
        help="Set the directory where you have Surf Scoter installed.",
    )
    parser.add_argument(
        "--bin-dir",
        action="store",
        dest="bin_dir",
        help=(
            "Point to the directory storing the Surf Scoter binary files.  "
            "Defaults to the 'bin' directory inside the distribution."
        ),
    )


def _read_user_input(
    prompt: str,
    interactive: bool,
    default: str,
) -> str | None:
    """Used for reading simple multiple-choice questions, not free-form text."""
    if interactive:
        try:
            line = input(prompt + " ")
        except EOFError:
            return None
        line = line.strip()
        if line:
            return line.lower()
    return default


# ---------------------------------------------------------------------
# CLI code.

def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    # Configure how tool accepts arguments.
    parser = argparse.ArgumentParser(
        prog=argv[0],
        description="Surf Scoter development environment maintenance.",
    )
    _add_settings_args(parser)
    subs = parser.add_subparsers()
    for name, populator in SUB_COMMAND_ARGUMENTS.items():
        populator(subs.add_parser(name))

    # Parse the arguments.
    args = parser.parse_args(argv[1:])

    # Run the sub-command selected.
    if not hasattr(args, "func"):
        print(parser.description)
        print()
        print("Run with '--help' to learn how to use the tool.")
        return 1
    try:
        return args.func(args)
    except ConfigErr as err:
        print(f"Configuration problem: {err}", file=sys.stderr)
    except subprocess.CalledProcessError as err:
        print("Failed running command", file=sys.stderr)
        print(str(err), file=sys.stderr)
    except KeyboardInterrupt:
        print("User interrupted execution.", file=sys.stdout)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
