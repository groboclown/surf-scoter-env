#!/usr/bin/env python3
"""The surf-scoter user front-end.

This parses the configuration setting arguments, processes them through the
universal configuration file generator, then runs the corresponding
distribution script for processing.
"""
# Compatibility: Python 3.12+
import argparse
import json
import os
import platform
import shutil
import subprocess  # nosec: B404  # we know the issues around subprocess.
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

JsonData = str | int | float | bool | None | list["JsonData"] | dict[str, "JsonData"]
DictJsonData = dict[str, JsonData]


# The few hard-coded items.
DISTRIBUTION_BIN_DIR = "bin"
SCOTER_HOST_EXEC = "scoter-host"
GEN_SCOTER_UC_EXEC = "gen-scoter-uc"
# The SUC's record of the project file, as written.
CURRENT_PROJECT_SETUP = "current-project-setup"
SUB_CMD_PREFIX = "main_"
SUB_CMD_SUFFIX = ".py"
VCS_SECTION_SUFFIX = "-vcs"


# ---------------------------------------------------------------------
# Configuration Understanding


class ConfigErr(Exception):
    """A problem with the configuration file or arguments that control it."""


def load_config(args: argparse.Namespace) -> DictJsonData:
    """Load the SUC based on the arguments."""
    bin_dir = args.bin_dir
    distribution_dir = args.distribution_dir

    dist_dir = require_global_config_path(distribution_dir)
    resolved_bin_dir = find_bin_dir(bin_dir, dist_dir)
    # Phase 1: the setup alone, which says how to find the project.
    settings = generate_config(
        bin_dir=resolved_bin_dir,
        distribution_dir=dist_dir,
        project_dir=None,
        dev_container_workspace_dir=None,
        dev_container_settings_dir=None,
        universal_file=None,
        no_setup_search=False,
    )

    # Look from `--project-dir`, or the current directory, up to the root of
    # the repository holding it.
    start = Path(os.path.abspath(args.project_dir or os.getcwd()))
    vcs_root = find_vcs_root(settings, start)
    project_dir = find_project_dir(settings, start, vcs_root[0] if vcs_root else None)

    if project_dir:
        # Phase 2: with the project, and the repository it's in.
        settings = reprocess_config(
            resolved_bin_dir,
            dist_dir,
            project_dir,
            vcs_root[0] if vcs_root else None,
            settings,
        )
        check_project_vcs(settings, vcs_root)
        # Phase 3: with the user's accepted settings, if they accepted it.
        found = find_acceptance(settings)
        accepted = found.get("accepted-settings") if found else None
        notice = found.get("notice") if found else None
        if isinstance(notice, str):
            print(notice, file=sys.stderr)
        settings = reprocess_config(
            resolved_bin_dir,
            dist_dir,
            project_dir,
            vcs_root[0] if vcs_root else None,
            settings,
            accepted_settings=accepted if isinstance(accepted, str) else None,
        )
        if not isinstance(accepted, str):
            warn_unreviewed_commands(settings)

    # Overwrite the bin dir if it was explicitly set.
    active_locs = settings["active-config-locations"]
    if isinstance(active_locs, dict):
        active_locs["distribution-bin-dir"] = str(resolved_bin_dir)

    return settings


def vcs_find_root_commands(settings: DictJsonData) -> list[tuple[str, list[str]]]:
    """The setup's VCS sections that can find their root: (VCS name, `runs: find-root`)."""
    resolved = _opt_lookup(settings, "resolved")
    if not isinstance(resolved, dict):
        return []
    ret: list[tuple[str, list[str]]] = []
    for section in sorted(resolved):
        if not section.endswith(VCS_SECTION_SUFFIX):
            continue
        runs = _opt_lookup(settings, "resolved", section, "runs")
        if not isinstance(runs, dict) or "find-root" not in runs:
            continue
        name = section[:-len(VCS_SECTION_SUFFIX)]
        template = runs["find-root"]
        if template is None:
            raise ConfigErr(
                f"The setup's `{section}` `runs: find-root` has no value; check that "
                f"the programs it uses are installed."
            )
        ret.append((name, _command(template, f"{section}/runs/find-root", "{x:start}")))
    return ret


def find_vcs_root(settings: DictJsonData, start: Path) -> tuple[Path, str] | None:
    """The root of the repository holding `start`, and its VCS name.

    Each VCS section's `runs: find-root` is run with `start`; it prints the
    root, or nothing.  The nearest root wins, so a Git submodule is its own
    repository when `start` is inside it.  Two VCS with the same root is an
    error.
    """
    real_start = Path(os.path.realpath(start))
    found: list[tuple[Path, str]] = []
    for name, template in vcs_find_root_commands(settings):
        cmd = [str(start) if part == "{x:start}" else part for part in template]
        res = subprocess.run(  # nosec: B603  # The setup's command, as a list.
            cmd,
            stdout=subprocess.PIPE,
            check=False,
            shell=False,
            timeout=60,
        )
        if res.returncode != 0:
            raise ConfigErr(f"Couldn't find the {name} repository root (`find-root` failed).")
        out = res.stdout.decode("utf-8", errors="replace").strip()
        if not out:
            continue
        root = Path(os.path.realpath(out))
        if not Path(out).is_absolute() or not real_start.is_relative_to(root):
            raise ConfigErr(f"The {name} `find-root` gave '{out}', which doesn't hold {start}.")
        found.append((root, name))
    if not found:
        return None
    nearest = max(len(root.parts) for root, _ in found)
    at = [(root, name) for root, name in found if len(root.parts) == nearest]
    if len(at) > 1:
        names = ", ".join(name for _, name in at)
        raise ConfigErr(f"{at[0][0]} is the root of more than one VCS: {names}")
    return at[0]


def find_project_dir(
    settings: DictJsonData,
    start: Path,
    vcs_root: Path | None,
) -> Path | None:
    """Find the project directory, from `start` up to the VCS root.

    The project directory holds the setup's `project-files-dir`, with its
    `project-file-name` in it.  `start` and each directory above it are
    tried, up to and including `vcs_root`; never above it, so a project
    planted above the repository isn't found.  Without a VCS root, only
    `start` is tried.
    """
    files_dir = _opt_lookup(settings, "resolved", "scoter", "values", "project-files-dir")
    file_name = _opt_lookup(settings, "resolved", "scoter", "values", "project-file-name")
    if not isinstance(files_dir, str) or not isinstance(file_name, str):
        raise ConfigErr(
            "Required scoter/values/project-files-dir and project-file-name in the configuration."
        )
    real_start = Path(os.path.realpath(start))
    dirs = [real_start]
    if vcs_root is not None:
        real_root = Path(os.path.realpath(vcs_root))
        if real_start.is_relative_to(real_root):
            above = len(real_start.relative_to(real_root).parts)
            dirs = [real_start, *list(real_start.parents)[:above]]
    for directory in dirs:
        if (directory / files_dir / file_name).is_file():
            return directory
    return None


def check_project_vcs(settings: DictJsonData, vcs_root: tuple[Path, str] | None) -> None:
    """Check the project's `vcs` against the repository it was found in.

    A VCS with a `runs: find-root` must be the one whose root was found.
    One without (such as `file`) can be used anywhere.
    """
    vcs = _opt_lookup(settings, CURRENT_PROJECT_SETUP, "vcs")
    if not isinstance(vcs, str):
        return
    rooted = {name for name, _ in vcs_find_root_commands(settings)}
    if vcs not in rooted:
        return
    if vcs_root is None:
        raise ConfigErr(
            f"The project's `vcs` is '{vcs}', but it isn't in a {vcs} repository."
        )
    if vcs_root[1] != vcs:
        raise ConfigErr(
            f"The project's `vcs` is '{vcs}', but it's in a {vcs_root[1]} "
            f"repository ({vcs_root[0]})."
        )


def find_acceptance(settings: DictJsonData) -> DictJsonData | None:
    """Ask the setup's `verified: runs: find-acceptance` for the project's acceptance.

    It's given the configuration with the project (`{x:universal}`), and
    prints JSON: `match`, `acceptance`, `accepted-settings` (the file for
    gen-scoter-uc) and `notice`.  None when the setup has no such command.
    """
    template = _opt_lookup(settings, "resolved", "verified", "runs", "find-acceptance")
    if template is None:
        return None
    if not isinstance(template, list) or not all(isinstance(a, str) for a in template):
        raise ConfigErr("The setup's `verified` `runs: find-acceptance` has no value.")
    tmp = save_to_tmp(settings)
    try:
        args = [arg.replace("{x:universal}", tmp) for arg in template]
        res = subprocess.run(  # nosec: B603  # The command comes from the user's setup.
            args, stdout=subprocess.PIPE, check=False, shell=False,
        )
    finally:
        remove_tmp(tmp)
    if res.returncode != 0:
        raise ConfigErr("Couldn't look up your acceptance of the project's settings.")
    try:
        found: JsonData = json.loads(res.stdout)
    except ValueError as err:
        raise ConfigErr(f"`find-acceptance` printed something that isn't JSON: {err}") from err
    if not isinstance(found, dict):
        raise ConfigErr("`find-acceptance` printed something unexpected.")
    return found


def warn_unreviewed_commands(settings: DictJsonData) -> None:
    """Warn that the project's host commands run without a review.

    Until the review of a project's settings exists (steps 8 to 16 of
    docs/contributors/projects-v1.md), the generator builds the commands
    from the project file as it is (`--unreviewed-project-commands`).
    """
    commands = settings.get("host-brokered")
    if not isinstance(commands, list) or not commands:
        return
    names = sorted({str(c.get("command")) for c in commands if isinstance(c, dict)})
    print(
        "Warning: the project's host commands ("
        + ", ".join(names)
        + ") come straight from its project file; Surf Scoter can't review them yet.  "
        "Read the file's `commands` before you run it.",
        file=sys.stderr,
    )


def reprocess_config(
    bin_dir: Path,
    dist_dir: Path,
    project_dir: Path,
    vcs_root: Path | None,
    orig: DictJsonData,
    *,
    accepted_settings: str | None = None,
) -> DictJsonData:
    """Reprocess this configuration with the project, and the repository it's in.

    With `accepted_settings`, the generator builds the host commands from them.
    """
    # Save the configuration for reprocessing.
    tmp = save_to_tmp(orig)

    try:
        return generate_config(
            bin_dir=bin_dir,
            distribution_dir=dist_dir,
            project_dir=str(project_dir),
            vcs_root=str(vcs_root) if vcs_root else None,
            dev_container_workspace_dir=None,
            dev_container_settings_dir=None,
            universal_file=tmp,
            no_setup_search=True,
            accepted_settings=accepted_settings,
        )
    finally:
        remove_tmp(tmp)


def save_to_tmp(data: DictJsonData) -> str:
    """Save the data to a private temporary file; remove it with `remove_tmp`.

    The universal configuration file decides what runs on this computer, so
    it goes in a new directory only you can use (`mkdtemp`), as a file only
    you can read (`mkstemp`), never in a directory others share.
    """
    directory = tempfile.mkdtemp(prefix="surf-scoter-")
    try:
        fd, file = tempfile.mkstemp(dir=directory, suffix=".json")
        try:
            os.write(fd, json.dumps(data).encode("utf-8"))
        finally:
            os.close(fd)
        return file
    except:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def remove_tmp(file: str) -> None:
    """Remove a file `save_to_tmp` made, and its directory."""
    shutil.rmtree(os.path.dirname(file), ignore_errors=True)


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

    # Try to find it based on the location of this script file.  That's the
    # tool the user chose to run, so it's passed on as the given distribution
    # directory, the same as '--distribution-dir'.
    this = os.path.realpath(__file__)
    if os.path.isfile(this):
        this_dir = os.path.dirname(this)
        # All we care about here is that the executables exist.
        bin_dir = Path(this_dir) / DISTRIBUTION_BIN_DIR
        if _is_exe_file(bin_dir / SCOTER_HOST_EXEC) and _is_exe_file(bin_dir / GEN_SCOTER_UC_EXEC):
            return Path(this_dir)

    # Couldn't find it.
    raise ConfigErr(
        "Could not find the Surf Scoter distribution directory.  "
        "You will need to pass it with the '--distribution-dir=PATH' argument."
)


def find_bin_dir(bin_dir: str | None, dist_dir: Path) -> Path:
    """Find the bin/ directory."""
    if bin_dir:
        if not os.path.isdir(bin_dir):
            raise ConfigErr(f"Could not find bin directory '{bin_dir}'")
        return Path(bin_dir)
    return dist_dir / DISTRIBUTION_BIN_DIR


def _opt_lookup(data: DictJsonData, *parts: str) -> JsonData:
    """Lookup the keys down the path of dictionaries."""
    val: JsonData = data
    for part in parts:
        if not isinstance(val, dict):
            raise ConfigErr(f"Required {'/'.join(parts)} in the configuration.")
        if part not in val:
            return None
        val = val[part]
    return val

def _dist_bin_dir(config: DictJsonData) -> Path:
    """Get the distribution bin directory stored in the configuration."""
    ret = _opt_lookup(config, "active-config-locations", "distribution-bin-dir")
    if not isinstance(ret, str):
        # The generator must set this.
        raise TypeError(f"distribution-bin-dir not set ({ret})")
    return Path(ret)


# ---------------------------------------------------------------------
# Generate the SUC file

def generate_config(
    *,
    bin_dir: Path | None,
    distribution_dir: Path,
    project_dir: str | None,
    dev_container_workspace_dir: str | None,
    vcs_root: str | None = None,
    dev_container_settings_dir: str | None,
    universal_file: str | None,
    no_setup_search: bool,
    accepted_settings: str | None = None,
) -> DictJsonData:
    """Run the gen-scoter-uc program.

    With a project, the host commands come from `accepted_settings` (the
    user's accepted settings); without them, from the project file, unreviewed.
    """
    if bin_dir:
        gen_uc = _require_exe(bin_dir / GEN_SCOTER_UC_EXEC)
    else:
        gen_uc = _require_exe(distribution_dir / DISTRIBUTION_BIN_DIR / GEN_SCOTER_UC_EXEC)
    args: list[str] = [str(gen_uc), "--distribution-dir", str(distribution_dir)]
    if project_dir:
        args.extend(("--project-dir", project_dir))
    if vcs_root:
        args.extend(("--vcs-root", vcs_root))
    if dev_container_workspace_dir:
        args.extend(("--dev-container-workspace-dir", dev_container_workspace_dir))
    if dev_container_settings_dir:
        args.extend(("--dev-container-settings-dir", dev_container_settings_dir))
    if universal_file:
        args.extend(("--universal", universal_file))
    if no_setup_search:
        args.append("--no-setup-search")
    if accepted_settings:
        args.extend(("--accepted-settings", accepted_settings))
    elif project_dir:
        # No acceptance yet: until a review can make one (step 15), the host
        # commands come from the project file; see `warn_unreviewed_commands`.
        args.append("--unreviewed-project-commands")
    res = subprocess.run(  # nosec: B603  # All input is validated.
        args,
        stdout=subprocess.PIPE,
        # let stderr be passed directly to the user.
        check=False,
        shell=False,
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


def _is_exe_file(path: Path) -> bool:
    """Whether 'path' is an executable file (with '.exe' added, on Windows)."""
    if _is_windows():
        return path.is_file() or path.with_name(path.name + ".exe").is_file()
    return path.is_file() and os.access(path, os.X_OK)


def _require_exe(name: str | Path) -> Path:
    """Require that the executable exists."""
    ret = _find_exe(name)
    if not ret:
        raise ConfigErr(f"could not find '{name}'")
    return ret


def _is_windows() -> bool:
    return platform.system() == "Windows"


# ---------------------------------------------------------------------
# Sub Command Handlers

def list_sub_commands(config: DictJsonData, script_dir: str | None) -> list[str]:
    """Get the list of all sub-commands."""
    if script_dir:
        bin_dir = Path(script_dir)
    else:
        bin_dir = _dist_bin_dir(config)
    ret: list[str] = []
    for name in os.listdir(bin_dir):
        if name.startswith(SUB_CMD_PREFIX) and name.endswith(SUB_CMD_SUFFIX):
            name = name[len(SUB_CMD_PREFIX):-len(SUB_CMD_SUFFIX)].replace("_", "-")
            ret.append(name)
    return ret


def _command(template: JsonData, where: str, placeholder: str) -> list[str]:
    """A `runs` command from the configuration, which must use `placeholder`
    and start with a program found as an absolute path."""
    if not isinstance(template, list):
        raise ConfigErr(f"Required {where} to be a command in the configuration.")
    parts = [part for part in template if isinstance(part, str)]
    if not parts or len(parts) != len(template):
        raise ConfigErr(f"Required {where} to be a command in the configuration.")
    if placeholder not in parts:
        raise ConfigErr(f"The setup's {where} must include `{placeholder}`.")
    if not os.path.isabs(parts[0]):
        raise ConfigErr(
            f"The setup's {where} must start with a program, found as an absolute path, "
            f"not '{parts[0]}'."
        )
    return parts


def sub_command_prefix(config: DictJsonData, cmd_file: Path) -> list[str]:
    """The start of the command line that runs the sub-command script 'cmd_file'.

    It's the setup's `python` `runs: sub-command`, with its `{x:script}` the
    script.  The universal configuration file and the sub-command name and
    arguments go after it.  The interpreter is the setup's `python`
    `programs: exec`, as found when the configuration was made.
    """
    template = _opt_lookup(config, "resolved", "python", "runs", "sub-command")
    if template is None:
        raise ConfigErr(
            "No Python interpreter to run the sub-command with: the setup's "
            "`python` `runs: sub-command` has no value.  Check that `python` "
            "`programs: exec` names an interpreter that's installed."
        )
    cmd = _command(template, "`python` `runs: sub-command`", "{x:script}")
    return [str(cmd_file) if part == "{x:script}" else part for part in cmd]


def run_sub_command(
    config: DictJsonData,
    argv: Sequence[str],
    script_dir: str | None,
) -> int:
    """Run the sub-command."""
    if script_dir:
        bin_dir = Path(script_dir)
    else:
        bin_dir = _dist_bin_dir(config)
    cmd_file = bin_dir / (SUB_CMD_PREFIX + argv[0].replace("-", "_") + SUB_CMD_SUFFIX)
    if not cmd_file.is_file():
        print(f"Unknown command '{argv[0]}'.", file=sys.stderr)
        print("Run with 'help' for a full list of commands.", file=sys.stderr)
        return 1

    # Checked before the configuration is saved, so a problem leaves no file.
    prefix = sub_command_prefix(config, cmd_file)
    config_file = save_to_tmp(config)
    cmd_args = [*prefix, config_file, *argv]

    # Run the sub-command, then remove the configuration it was given.
    try:
        return run_to_end(cmd_args)
    except OSError as err:
        print(f"Failed to start sub-command {argv[0]} at {cmd_file}: {err}", file=sys.stderr)
        return 127
    finally:
        remove_tmp(config_file)


def run_to_end(cmd_args: Sequence[str]) -> int:
    """Run the command, and wait for it to end; its exit code.

    Ctrl-C reaches the command too (it's in the same process group), and the
    command handles it: `run` tears its environment down.  So this keeps
    waiting rather than stopping it, as `subprocess.run` would.
    """
    process = subprocess.Popen(cmd_args, shell=False)  # nosec: B603  # Vetted by the caller.
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            continue


# ---------------------------------------------------------------------
# Command and Argument helpers

def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    """Parse the command-line arguments.

    Returns:
        0: this command's arguments.
        1: the sub-command.
        2: the sub-command arguments.
    """
    if len(argv) <= 0:
        raise RuntimeError("You must run this with the program name as the first argument.")
    cmd = os.path.basename(argv[0])

    parser = argparse.ArgumentParser(
        prog=cmd,
        description="Surf Scoter development environment maintenance.",
    )

    parser.add_argument(
        "-d",
        "--project-dir",
        action="store",
        dest="project_dir",
        help=(
            "Where to look for the project: the project directory, or one under it.  "
            "Defaults to the current directory."
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
    parser.add_argument(
        "--sub-cmd-dir",
        action="store",
        dest="sub_cmd_dir",
        help=(
            "Point to the directory storing the Surf Scoter sub-command files.  "
            "Defaults to the 'bin' directory inside the distribution."
        ),
    )

    parser.add_argument(
        "cmd",
        help="A sub-command.  Use 'help' for a full list of commands you can run.",
    )
    parser.add_argument(
        "cmd_args",
        # This is the magic.
        nargs=argparse.REMAINDER,
        #help="Arguments for the sub-command.",
    )

    res = parser.parse_args(argv[1:])
    return res


# ---------------------------------------------------------------------
# CLI code.

def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    # Configure how tool accepts arguments.
    try:
        args = parse_arguments(argv)
        config = load_config(args)
    except ConfigErr as err:
        print(f"Configuration error: {err}", file=sys.stderr)
        return 1

    sub_cmd = args.cmd
    sub_cmd_args = args.cmd_args
    if not sub_cmd or sub_cmd == "help":
        print("All supported sub-commands:")
        for sub in list_sub_commands(config, args.sub_cmd_dir):
            print(f"  {sub}")
        return 0
    if sub_cmd == "show-suc":
        json.dump(config, sys.stdout)
        return 0

    try:
        return run_sub_command(config, [sub_cmd, *sub_cmd_args], args.sub_cmd_dir)
    except ConfigErr as err:
        print(f"Configuration error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
