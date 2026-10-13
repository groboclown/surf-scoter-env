#!/usr/sbin/env python3
"""The surf-scoter user tool.


"""
# Compatibility: Python 3.12+

# TODO redo the configuration loading:
#      1. Find the install directory.  Either it's passed as '--install-dir',
#         or find it from the script's parent directory if it has the necessary
#         binaries in the bin/ directory.
#      2. Run the bin/gen-scoter-uc program with the right arguments.
#         Capture stdout to pull in the JSON document.  stderr is passed to the
#         user.


import argparse
from collections.abc import Callable, Sequence
import configparser
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import sys

SCOTER_DEFAULT_INI_FILE = "surf-scoter.ini"
CONTAINERS_DEFAULT_DIR = "containers"
DEFAULTS_DEFAULT_DIR = "defaults"
BACKUP_FILE_EXT = ".backup"

ENV_SCOTER_HOME = "SURF_SCOTER_HOME"


# ---------------------------------------------------------------------
# Configuration Understanding

class IniError(Exception):
    """An ini file error."""


class UserConfig:
    """Knows how to deal with the OS and user-wide configuration directory/files."""

    def __init__(self, config: Path | None, global_config: Path | None) -> None:
        self._user_config = config
        self._global_config = global_config
        self._dir_search_order: list[Path] = []
        self._cache_loaded = False
        self._cache: configparser.ConfigParser | None = None

    def tool_path(self, name: str) -> Path | None:
        """Find the executable for the tool, as set in the configuration."""
        lookup = self._lookup(name, "exe")
        tries = [lookup, None if not lookup else shutil.which(lookup), shutil.which(name)]
        for item in tries:
            if not item:
                continue
            p_val = Path(item)
            if p_val.is_file():
                return p_val
        return None

    def parsed_template(self, section: str, key: str, params: dict[str, str]) -> str | None:
        """Parse the key using extra parameters."""
        return self._lookup(section, key, params)

    def get_str(self, section: str, key: str, default: str) -> str:
        """Extract the key from the configuration."""
        ret = self._lookup(section, key)
        if ret is None:
            return default
        return ret

    def require_str(self, section: str, key: str) -> str:
        """Extract the key from the configuration."""
        ret = self._lookup(section, key)
        if ret is None:
            raise RuntimeError(f"missing key {section}:key")
        return ret

    def get_path(self, section: str, key: str, default: str | Path) -> Path:
        """Get the key as a path."""
        ret = self._lookup(section, key)
        if ret is not None:
            return Path(os.path.expanduser(ret))
        return Path(os.path.expanduser(default))

    def require_path(self, section: str, key: str) -> Path:
        """Get the key as a path."""
        ret = self._lookup(section, key)
        if ret is None:
            raise IniError(f"missing key {section}:{key} from your ini files")
        return Path(os.path.expanduser(ret))

    def _lookup(
        self,
        section: str,
        key: str,
        extra: dict[str, str] | None = None,
        parents: set[str] | None = None,
    ) -> str | None:
        """Look in the configuration files for the section/key."""
        parents = parents or set()
        joined = section + ":" + key
        if joined in parents:
            raise IniError(f"the keys in your ini files include self-referencing in the collection {parents}")
        parents = {*parents, joined}
        self._load_cache()
        if self._cache and section in self._cache:
            value = self._cache[section].get(key)
            if value is None:
                return None
            # Parse the {[section:]key} format.
            # This is fun-time recursion.
            res = str(value)
            ret = ""
            while True:
                pos1 = res.find("{")
                if pos1 < 0:
                    return ret + res
                ret += res[:pos1]
                res = res[pos1+1:]
                if not res or res[0] == "{":
                    # Either '{' at the end, or it's a {{ (escaped).
                    ret += "{"
                    res = res[1:]
                    continue
                pos2 = res.find("}")
                if pos2 < 0:
                    # Unmatched '{'
                    return ret + "{" + res
                raw_key = res[:pos2]
                res = res[pos2+1:]
                pos3 = raw_key.find(":")
                if pos3 >= 0:
                    r_sec = raw_key[:pos3]
                    r_key = raw_key[pos3+1:]
                else:
                    r_sec = section
                    r_key = raw_key
                sub: str | None
                if extra and r_key in extra:
                    sub = extra[r_key]
                else:
                    sub = self._lookup(r_sec, r_key, extra, parents)
                if sub is not None:
                    ret += sub
                else:
                    ret += "{" + raw_key + "}"
        return None

    def _load_cache(self) -> None:
        """Load the cached parameters.
        
        This is a bit of recursion, in that it runs _lookup.  To
        avoid that, this sets _cache_loaded to True at the start.

        See the 'docs/user-ini-file.md' for a description of the
        load order.
        """
        if not self._cache_loaded:
            self._cache_loaded = True

            # Need to get the INI file order,
            # and define the directory search order (for template files).
            # This is listed in reverse priority.

            def or_file(value: Path | str | None) -> str | None:
                if not value:
                    return None
                return os.path.join(str(value), SCOTER_DEFAULT_INI_FILE)

            def or_defaults(value: Path | str | None) -> str | None:
                if not value:
                    return None
                return os.path.join(str(value), DEFAULTS_DEFAULT_DIR)

            def filter_files(vals: Sequence[Path | str | None]) -> list[str]:
                ret: list[str] = []
                for val in vals:
                    if not val:
                        continue
                    expand = str(val)
                    if not expand:
                        continue
                    expand = os.path.expanduser(expand)
                    if os.path.isfile(expand):
                        # TODO put file size limits on this.
                        ret.append(expand)
                return ret

            initial_order = filter_files((
                # "defaults" directories are lower priority than the other locations.
                or_file(or_defaults(self._global_config)),
                or_file(Path.home() / ".config" / "scoter.d" / DEFAULTS_DEFAULT_DIR),
                or_file(or_defaults(os.environ.get("SURF_SCOTER_HOME"))),

                # System-wide globals.
                or_file(self._global_config),
                self._global_config,

                # User home config directory.
                or_file(Path.home() / ".config" / "scoter.d"),
                or_file(Path.home() / ".config"),

                # ENV location.
                or_file(os.environ.get("SURF_SCOTER_HOME")),

                # CLI argument for the configuration.
                # Can point at a file or a directory.
                self._user_config,
                or_file(self._user_config),
            ))
            if initial_order:
                self._cache = configparser.ConfigParser()
                self._cache.read(initial_order)

                # The follow-up defaults come from those files read so far.
                # From least important to most important
                install_dir = self._lookup("scoter", "install-dir")
                user_dir = self._lookup("scoter", "user-dir")
                self._cache = configparser.ConfigParser()
                self._cache.read(filter_files((
                    # "defaults" directories are lower priority than the other locations.
                    or_file(or_defaults(install_dir)),
                    or_file(or_defaults(self._global_config)),
                    or_file(Path.home() / ".config" / "scoter.d" / DEFAULTS_DEFAULT_DIR),
                    or_file(or_defaults(os.environ.get("SURF_SCOTER_HOME"))),
                    or_file(or_defaults(user_dir)),

                    # Install directory.
                    or_file(install_dir),

                    # System-wide globals.
                    or_file(self._global_config),
                    self._global_config,

                    # User home config directory.
                    or_file(Path.home() / ".config" / "scoter.d"),
                    or_file(Path.home() / ".config"),

                    # ENV location.
                    or_file(os.environ.get("SURF_SCOTER_HOME")),

                    # Declared user directory.
                    or_file(user_dir),

                    # CLI argument for the configuration.
                    # Can point at a file or a directory.
                    self._user_config,
                    or_file(self._user_config),
                )))


def find_global_config_path() -> Path | None:
    """Find where the OS installed the global configuration path."""
    # For now, Surf Scoter only installs as a user application.
    # If, however, this script still sits in the install location, then
    # that's a hint.
    this = os.path.realpath(__file__)
    print(f"DEBUG looking for global path at {this}", file=sys.stderr)
    if os.path.isfile(this):
        this_dir = os.path.dirname(this)
        # All we care about here is the 'defaults' and 'containers' directories.
        if (
            os.path.isdir(os.path.join(this_dir, CONTAINERS_DEFAULT_DIR))
            and os.path.isdir(os.path.join(this_dir, DEFAULTS_DEFAULT_DIR))
        ):
            return Path(this_dir)
    return None

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
def _backup(file: Path) -> bool:
    """Backup the file.
    
    Returns 'true' if the backup was successful.
    """
    if not file.is_file():
        # The file to back up doesn't exist, so there's nothing to back up.
        # This is fine.
        return True
    to_file = Path(str(file) + BACKUP_FILE_EXT)
    if to_file.is_file():
        to_file.unlink()
    if not to_file.exists():
        file.copy(to_file, preserve_metadata=True)
        return True
    return False


# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# Sub Command Handlers
# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# Each sub-command handler takes the arguments: (UserConfig, argparse.Namespace)
# and returns int.
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


def user_space_setup_runner(config: UserConfig, args: argparse.Namespace) -> int:
    """Run the user-space setup."""
    ssh_dir = config.require_path("ssh", "home")
    interactive = bool(args.interactive)

    return 0


SUB_COMMAND_ARGUMENTS["user-setup"] = user_space_setup_parser


# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# External Tool Code
# ---------------------------------------------------------------------
# ---------------------------------------------------------------------




# ---------------------------------------------------------------------
# CLI code.

def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    # Configure how tool accepts arguments.
    parser = argparse.ArgumentParser(
        prog=argv[0],
        description="Surf Scoter development environment maintenance.",
    )
    parser.add_argument(
        "-U",
        "--user-config",
        dest="user_config",
        help=(
            "Set your user configuration directory or file.  "
            "Also set through the SURF_SCOTER_HOME environment variable."
        ),
        action="store",
    )
    subs = parser.add_subparsers()
    for name, populator in SUB_COMMAND_ARGUMENTS.items():
        populator(subs.add_parser(name))

    # Parse the arguments.
    args = parser.parse_args(argv[1:])
    global_config = UserConfig(Path(args.user_config_dir), find_global_config_path())

    # Run the sub-command selected.
    if not hasattr(args, "func"):
        print(parser.description)
        print("Run with '--help' to learn how to use the tool.")
        return 1
    return args.func(global_config, args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
