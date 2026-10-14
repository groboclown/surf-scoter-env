#!/usr/sbin/env python3
"""The surf-scoter user tool."""
# Compatibility: Python 3.12+


import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import shlex
import sys

JsonData = str | int | float | bool | None | list["JsonData"] | dict[str, "JsonData"]
DictJsonData = dict[str, JsonData]
ListJsonData = list[JsonData]
JsonPathPart = str | int


# The few hard-coded items.
DISTRIBUTION_BIN_DIR = "bin"
SCOTER_HOST_EXEC = "scoter-host"
GEN_SCOTER_UC_EXEC = "gen-scoter-uc"


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
        check: bool = True,
        capture_stdout: bool = True,
        template: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run the given command.
        
        This will return the completed process with stdout captured.  stderr will
        forward to the user.
        """
        return subprocess.run(
            self.require_run(section, run, template),
            stdout=subprocess.PIPE if capture_stdout else None,
            check=check,
        )

    def dev_container_str(self, key: str) -> str:
        """Get the dev-container key."""
        val = self.__opt_str("dev-container", key)
        if val is None:
            raise self.__key_err(("dev-container", key), "dev-env.yaml must provide this value")
        return val

    def dev_container_mounts(self) -> dict[str, str]:
        """Get the dev-container volume mount points."""
        val = self.__get(("dev-container", "mounts"))
        if not isinstance(val, dict):
            return {}
        return val

    def project_str(self, *key: JsonPathPart) -> str | None:
        """Get the project key."""
        val = self.__opt_str("project", *key)
        if val is None:
            return None
        return str(val)

    def save(self, outfile: str) -> None:
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
                user_setup=None,
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
        user_setup: str | None,
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
        user_setup: str | None,
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
        if not exists:
            return None
        if isinstance(obj, str):
            return obj
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
# Basic file operations
def _backup(file: Path, backup_ext: str) -> bool:
    """Backup the file.
    
    Returns 'true' if the backup was successful.
    """
    if not file.is_file():
        # The file to back up doesn't exist, so there's nothing to back up.
        # This is fine.
        return True
    to_file = Path(str(file) + backup_ext)
    if to_file.is_file():
        to_file.unlink()
    if not to_file.exists():
        file.copy(to_file, preserve_metadata=True)
        return True
    return False


# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# External Tool Code
# ---------------------------------------------------------------------
# ---------------------------------------------------------------------


def generate_config(
    *,
    bin_dir: Path | None,
    distribution_dir: Path,
    project_config_file: str | None,
    project_dir: str | None,
    dev_container_workspace_dir: str | None,
    dev_container_settings_dir: str | None,
    user_setup: str | None,
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
    if user_setup:
        args.extend(("--user-setup", user_setup))
    if universal_file:
        args.extend(("--universal", universal_file))
    if no_setup_search:
        args.append("--no-setup-search")
    res = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        # let stderr be passed directly to the user.
        check=True,
    )
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
# Start a dev environment.

def start_env_parser(parser: argparse.ArgumentParser) -> None:
    """Configure the CLI arguments."""
    parser.description = "Start the project dev container."
    parser.set_defaults(func=start_env_runner)
    parser.add_argument(
        "--vs-settings",
        action="store",
        dest="vs_settings",
        help="Update your VS user settings file to run in this container.",
    )


SUB_COMMAND_ARGUMENTS["start"] = start_env_parser


def start_env_runner(args: argparse.Namespace) -> int:
    """Start the dev environment."""
    config = UserConfig.load_project_from_args(args)

    # Setup the SSH config file for running.
    host_prefix = _setup_ssh_config(config)
    if not host_prefix:
        return 1

    # FIXME

    # Discover the host suffix to use.

    # Build the dev-env container + firewall-router + network-gateway.

    # Setup the workspace directory via the vcs command.

    # Setup the working directories by putting a vcs "ignore-file" in each, ignoring everything.

    # Setup the .env file in the resources directory.  Much of this comes from
    # the host suffix and the config "dev-env" section.

    # Setup the docker-compose file in the resources directory.

    # Copy the host key into the setup directory.

    # Write the authorized keys file into the setup directory.

    # Write the host-bridge command list into the setup directory.

    # Start the docker-compose.yaml (daemon mode).

    # Record the running compose containers IDs + the path, and
    # store those in the user's host store directory.  This is used
    # for assigning host suffixes, for use with parallel dev envs.

    # Capture KeyboardInterrupt to trigger stopping the compose.

    # Start scoter-host

    # Tell the user the setup for the container (host name).

    # Wait.

    return 0


def _setup_ssh_config(config: UserConfig) -> str | None:
    # Ensure the right SSH config host block exists.
    # It generates the host prefix.
    # This isn't 100% right, in that it doesn't check for all host matches.
    ssh_config = SshConfig.read_ssh_config(config)
    if not ssh_config:
        return None

    user = config.dev_container_str("user")
    port = config.dev_container_str("sshd-port")
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
        ssh_config_file.touch(0o600)

    return host_prefix


# ---------------------------------------------------------------------
# Stop a dev environment.

def stop_env_parser(parser: argparse.ArgumentParser) -> None:
    """Configure the CLI arguments."""
    parser.description = "Stop the project dev container.  The 'start' should linger around as long as the container is running.  Use this to stop run-away configurations."
    parser.set_defaults(func=stop_env_runner)
    parser.add_argument(
        "-h",
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
    return 0


def _gen_key_pair(config: UserConfig, interactive: bool) -> None:
    # Step 1: generate the key pair.
    key_file = config.require_path("ssh", "key-file")
    pub_key_file = Path(str(key_file) + ".pub")
    if key_file.is_file() and pub_key_file.is_file():
        answer = _read_user_input(
            f"The key file pair ({key_file}) already exists.  Replace? [yN]",
            interactive,
            "n",
        )
        if answer and answer[0] == "y":
            key_file.unlink()
            pub_key_file.unlink()
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
            shutil.rmtree(key_file, ignore_errors=True)
            shutil.rmtree(pub_key_file, ignore_errors=True)
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
            ssh_config_file.touch(0o600)
            print(f"Created an empty ssh config file ({ssh_config_file})")


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
        action="store",
        dest="user_config",
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
    except KeyboardInterrupt:
        print("User interrupted execution.", file=sys.stdout)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
