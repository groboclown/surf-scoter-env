#SPDX:MIT
"""The Surf Scoter Universal Configuration file handler."""
import enum
import json
import os
import subprocess  # nosec: B404  # We know the potential issues with subprocess.
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import fileutil, jdata
from .err import ConfigErr

# The few hard-coded items.
VCS_SECTION_SUFFIX = "-vcs"
# The SUC's record of the project file, as written.
CURRENT_PROJECT_SETUP = "current-project-setup"
# Where the `found` scope records the project, unparsed (`scoter.py`).
FOUND_PROJECT = "found-project"


class PathKind(enum.Enum):
    """What kind of file `UserConfig.search_path` looks for."""

    FILE = "file"
    DIR = "dir"


class OnError(enum.Enum):
    """What `UserConfig.run_command` does when the command fails."""

    # Raise `subprocess.CalledProcessError`.
    RAISE = "raise"
    # Give back the completed process, with its exit code, for the caller to check.
    RETURN = "return"


class Capture(enum.Flag):
    """Which of the command's output streams `UserConfig.run_command` captures.

    A stream it doesn't capture goes to the user.
    """

    NONE = 0
    STDOUT = enum.auto()
    STDERR = enum.auto()
    BOTH = STDOUT | STDERR


class Echo(enum.Enum):
    """Whether `UserConfig.run_command` says what it runs."""

    SHOW = "show"
    QUIET = "quiet"

# ---------------------------------------------------------------------
# Configuration Understanding

@dataclass(frozen=True)
class FoundProject:
    """Where `scoter` found the project, and the repository holding it."""

    project_dir: Path
    vcs_root: Path | None
    # The VCS whose root was found.
    vcs: str | None
    project_branch: str | None
    # How: by its files (`files`), by its link in the store (`link`), or
    # it's only where the search started (`start`), for `init`.
    found_by: str = "files"

    def generator_args(self) -> list[str]:
        """The generator's arguments for this project, with its files read from elsewhere."""
        args = ["--project-dir", str(self.project_dir)]
        if self.vcs_root is not None:
            args.extend(("--vcs-root", str(self.vcs_root)))
        if self.project_branch:
            args.extend(("--project-branch", self.project_branch))
        return args


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
        if self.__find(jdata.find_dict, "resolved", ret) is None:
            raise ConfigErr(f"VCS provider '{name}' not supported with the current configuration")
        return ret

    def search_path(
        self, section: str, key: str, file: str | None, kind: PathKind,
    ) -> Path | None:
        """Search the path list at the section/key for the file, of the given kind.

        If the file is None, then this just tries to find the first of the path list that
        exists.  If the section/key does not exist, this raises a ConfigErr.
        """
        exists = os.path.isdir if kind == PathKind.DIR else os.path.isfile
        for path in self.__req_list_keys("resolved", section, "path-lists", key):
            loc = self.__req_str(*path)
            if file is not None:
                loc = os.path.join(loc, file)
            if exists(loc):
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
        """A section's `values` entry, which must be set, as text.

        The generator keeps a number as a number (`cpus: 4`), so it's given
        back as its text.
        """
        val = self.__find(jdata.find, "resolved", section, "values", key)
        if isinstance(val, str):
            return val
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return str(val)
        if val is None:
            raise ConfigErr(f"The setup's `{section}` `values: {key}` isn't set.")
        raise ConfigErr(f"The setup's `{section}` `values: {key}` must be text or a number, not {val!r}.")

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

    def set_record(self, key: str, value: jdata.JsonData) -> None:
        """Set a top-level record the tools add to the file, such as the configuration a run uses.

        Never one the generator writes: those are its own.
        """
        if key in ("active-config-locations", "resolved", "setup", "host-command-checks",
                   "host-brokered", "host-file", CURRENT_PROJECT_SETUP, "dev-container"):
            raise ValueError(f"`{key}` is the generator's")
        self._config[key] = value

    def has_run(self, section: str, run: str) -> bool:
        """Whether the section has the `runs` command (a VCS needn't have them all)."""
        return jdata.find(self._config, "resolved", section, "runs", run) is not None

    def require_run(
        self, section: str, run: str, template: dict[str, str] | None = None,
        *, whole_args: bool = False,
    ) -> list[str]:
        """Return the required run command, with each `{x:KEY}` replaced by `template[KEY]`.

        With `whole_args`, a value is only ever a whole argument: an
        argument that's exactly `{x:KEY}` is replaced, and any other holding
        `{x:` is a `ConfigErr`, so an untrusted value can't be read as part
        of an option or a path.
        """
        args: list[str] = []
        for path in self.__req_list_keys("resolved", section, "runs", run):
            arg = self.__req_str(*path)
            if whole_args:
                key = arg[3:-1] if arg.startswith("{x:") and arg.endswith("}") else None
                if key is not None and key in (template or {}):
                    args.append((template or {})[key])
                    continue
                if "{x:" in arg:
                    raise ConfigErr(f"`{section}: runs: {run}`: {arg!a} must be a whole argument")
                args.append(arg)
                continue
            for key,val in (template or {}).items():
                arg = arg.replace("{x:" + key + "}", val)
            args.append(arg)
        return args

    def command_env(self, section: str) -> dict[str, str]:
        """The environment for the section's `runs` commands: this one, less its `drop-env`.

        A section's `value-lists: drop-env` names the variables its commands
        don't get, each a name, or a prefix ending in `*`: the VCS's own
        variables, say (`GIT_*`), which a project can set as you enter its
        directory (a `.envrc`), and which would point the VCS at another
        repository, configuration or program.
        """
        return drop_env(os.environ, self.value_list(section, "drop-env"))

    def run_command(
        self,
        section: str,
        run: str,
        /, *,
        cwd: Path | None = None,
        on_error: OnError = OnError.RAISE,
        capture: Capture = Capture.STDOUT,
        template: dict[str, str] | None = None,
        echo: Echo = Echo.SHOW,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run the given command.

        This will return the completed process with the `capture` streams
        captured (by default, stdout); the others go to the user.  With
        `Echo.QUIET`, it doesn't say what it runs.
        """
        cmd = self.require_run(section, run, template)
        if echo == Echo.SHOW:
            print(f"Running {cmd[0]}")
            print(f"DEBUG {cmd}")
        return subprocess.run(  # nosec: B603  # Input validated on the call.
            cmd,
            stdout=subprocess.PIPE if Capture.STDOUT in capture else None,
            stderr=subprocess.PIPE if Capture.STDERR in capture else None,
            check=on_error == OnError.RAISE,
            cwd=cwd,
            shell=False,
            env=self.command_env(section),
        )

    def dev_container_sshd_port_env(self) -> str | None:
        """Get the sshd-port-env from the dev-container."""
        return self.__opt_str("dev-container", "sshd-port-env")

    def dev_container_sshd_port(self) -> str | None:
        """The dev container's `sshd-port`, as text; None when it's null (chosen at run time).

        `dev-env.yaml` gives it as a number, and the run sets it as one.
        """
        val = self.__find(jdata.find, "dev-container", "sshd-port")
        if val is None:
            return None
        if isinstance(val, bool) or not isinstance(val, (int, str)):
            raise ConfigErr(f"bad universal configuration file: dev-container / sshd-port: {val!r} isn't a port")
        return str(val)

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
        """The dev container's mount points, by `mounts` key.

        The project file's `container: mounts: project-clone`, when it sets
        one, takes the place of the container's own, as in the generator.
        """
        ret: dict[str, str] = {}
        for path in self.__req_dict_keys("dev-container", "mounts"):
            ret[str(path[-1])] = self.__req_str(*path)
        project_clone = self.project_str("container", "mounts", "project-clone")
        if project_clone is not None:
            ret["project-clone"] = project_clone
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

    def simple_checks(self, group: str) -> dict[str, jdata.DictJsonData]:
        """The setup's `project-files` or `egress-hosts` checks, by `section:key`, as written."""
        found: dict[str, jdata.DictJsonData] = {}
        for path in self.__find(jdata.find_dict_keys, "resolved"):
            section = str(path[-1])
            for key_path in self.__find(jdata.find_dict_keys, "resolved", section, group):
                found[f"{section}:{key_path[-1]}"] = self.__req_dict(*key_path)
        return found

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

    def egress_rules(self) -> list[jdata.DictJsonData]:
        """The egress rules the gateway enforces (`egress.rules`): the configuration's; none without one."""
        return [self.__req_dict(*key) for key in self.__find(jdata.find_list_keys, "egress", "rules")]

    def project_egress(self) -> list[jdata.DictJsonData]:
        """The project file's own egress rules: only a suggestion, for the review."""
        return [
            self.__req_dict(*key)
            for key in self.__find(jdata.find_list_keys, CURRENT_PROJECT_SETUP, "egress")
        ]

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
            Path(p)
            for p in self.__find(jdata.find_str_list, "resolved", section, "path-lists", key)
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

    def found_project(self) -> FoundProject | None:
        """The project `scoter` found, unparsed (`found-project`); None without one.

        Only the `found` scope's sub-commands (`subcmd-project-found.txt`) get it.
        """
        found = self.__find(jdata.find_dict, FOUND_PROJECT)
        if found is None:
            return None

        def path(key: str) -> Path | None:
            value = self.__find(jdata.find_str, FOUND_PROJECT, key)
            return None if value is None else Path(value)

        project_dir = path("project-dir")
        if project_dir is None:
            raise ConfigErr(f"bad universal configuration file: {FOUND_PROJECT} has no project-dir")
        return FoundProject(
            project_dir=project_dir,
            vcs_root=path("vcs-root"),
            vcs=self.__find(jdata.find_str, FOUND_PROJECT, "vcs"),
            project_branch=self.__find(jdata.find_str, FOUND_PROJECT, "project-branch"),
            found_by=self.__find(jdata.find_str, FOUND_PROJECT, "found-by") or "files",
        )

    def host_file_path(self) -> Path | None:
        """The host file the generator loaded (`--host-file`); None without one."""
        file = self.__find(jdata.find_str, "host-file", "file")
        return None if file is None else Path(file)

    def host_file_programs(self) -> dict[str, jdata.DictJsonData] | None:
        """The host file's `programs`, by the name the commands use; None without a host file.

        The generator checked them against the configuration's commands and
        this computer when it loaded the file.
        """
        if self.__find(jdata.find_dict, "host-file") is None:
            return None
        return {
            str(path[-1]): self.__req_dict(*path)
            for path in self.__find(jdata.find_dict_keys, "host-file", "contents", "programs")
        }

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


def drop_env(environ: Mapping[str, str], patterns: Sequence[str]) -> dict[str, str]:
    """`environ` without the variables `patterns` name: each a name, or a prefix ending in `*`.

    Names match as the platform's environment does: ignoring case on Windows.
    """
    def fold(name: str) -> str:
        return name.upper() if fileutil.is_windows() else name

    exact = {fold(p) for p in patterns if not p.endswith("*")}
    prefixes = tuple(fold(p[:-1]) for p in patterns if p.endswith("*"))
    return {
        name: value for name, value in environ.items()
        if fold(name) not in exact and not (prefixes and fold(name).startswith(prefixes))
    }
