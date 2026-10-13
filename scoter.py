#!/usr/sbin/env python3
"""The surf-scoter user tool.


"""
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

    def _get_resolved(self, section: str) -> DictJsonData:
        """Find the resolved section group.
        
        If not found, this will return an empty section.
        """
        ret = self.__require_dict("resolved", section)
        if ret:
            return ret
        return {"paths": {}, "programs": {}, "runs": {}, "path-lists": {}, "values": {}}

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

    def __require_dict(self, *path: JsonPathPart) -> DictJsonData:
        obj, exists = self.__get(path)
        if exists and isinstance(obj, dict):
            return obj
        raise UserConfig.__key_err(path, "expected map")

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
    def _parse_line(host: str | None, line: str, line_no: int) -> "SshConfigLine":
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

    @staticmethod
    def output(by_host: "dict[str | None, list[SshConfigLine]]") -> str:
        """Convert the by-host collection into a Host list.
        
        This will correctly interweave the newly added lines into the configuration.
        """
        if not by_host:
            return ""
        max_lineno = max([max(n.line_no for n in h) for h in by_host.values()])

        def line_order(v: SshConfigLine) -> int:
            if v.line_no <= 0:
                # subtraction -> adding the two together.
                return max_lineno + 1 - v.line_no
            return v.line_no

        host_order = sorted(
            by_host.keys(),
            # The host line is always first in the host block.
            # In the case of the None host, it's always first in the file, if it exists.
            key=lambda a: line_order(by_host[a][0]),
        )
        ret = ""

        for host in host_order:
            lines = sorted(by_host[host], key=line_order)
            ret += "".join([n.line for n in lines])

        return ret

    @staticmethod
    def parse_file(contents: str) -> "dict[str | None, list[SshConfigLine]]":
        """Parse a whole ssh_config file."""
        ret: dict[str | None, list[SshConfigLine]] = {}
        host: str | None = None
        line_no = 0
        for line in contents.splitlines(keepends=True):
            line_no += 1
            parsed = SshConfigLine._parse_line(host, line, line_no)
            host = parsed.host
            lines = ret.get(parsed.host)
            if not lines:
                lines = []
                ret[parsed.host] = lines
            lines.append(parsed)
        return ret


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
    # Which will work if you pass it the absolute 
    loc = shutil.which(name)
    if loc:
        # If 'name' is relative, which will return a relative path.
        return Path(os.path.abspath(loc))
    if platform.system() == "Windows":
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


# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# Sub Command Handlers
# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# Each sub-command handler has the signature (argparse.Namespace) -> int
# They also register an argument parser to the 'SUB_COMMAND_ARGUMENTS' value.
SUB_COMMAND_ARGUMENTS: dict[str, Callable[[argparse.ArgumentParser], None]] = {}

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


def user_space_setup_runner(args: argparse.Namespace) -> int:
    """Run the user-space setup."""
    config = UserConfig.load_settings_from_args(args)
    ssh_dir = config.require_path("ssh", "home")
    interactive = bool(args.interactive)

    return 0


SUB_COMMAND_ARGUMENTS["user-setup"] = user_space_setup_parser

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


def configuration_debug_runner(args: argparse.Namespace) -> int:
    """Output the SUC to stdout."""
    if args.use_project:
        config = UserConfig.load_project_from_args(args)
    else:
        config = UserConfig.load_settings_from_args(args)
    print(config.dumps())
    return 0


SUB_COMMAND_ARGUMENTS["parse-config"] = configuration_debug_parser


# ---------------------------------------------------------------------
# Argument helpers

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
        help="Point to the directory storing the Surf Scoter binary files.",
    )


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
