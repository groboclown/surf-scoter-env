#!/usr/sbin/env python3
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
SUB_CMD_PREFIX = "main_"
SUB_CMD_SUFFIX = ".py"


# ---------------------------------------------------------------------
# Configuration Understanding


class ConfigErr(Exception):
    """A problem with the configuration file or arguments that control it."""


def load_config(args: argparse.Namespace) -> DictJsonData:
    """Load the SUC based on the arguments."""
    bin_dir = args.bin_dir
    distribution_dir = args.distribution_dir
    user_setup = args.user_config
    project_config_file = args.project_config
    project_dir = args.project_dir

    dist_dir = require_global_config_path(distribution_dir)
    resolved_bin_dir = find_bin_dir(bin_dir, dist_dir)
    settings = generate_config(
        bin_dir=resolved_bin_dir,
        distribution_dir=dist_dir,
        project_config_file=project_config_file,
        project_dir=project_dir,
        dev_container_workspace_dir=None,
        dev_container_settings_dir=None,
        user_setup=user_setup,
        universal_file=None,
        no_setup_search=False,
    )

    # The user may want the project configuration.
    if not _opt_resolved_path(settings, "scoter", "project-config"):
        # It wasn't, so use the search path to find it.
        project_file = _opt_search_path(settings, "scoter", "project-file-search")
        if project_file:
            # Reload the setup with that discovered path.
            settings = reprocess_config(
                resolved_bin_dir,
                distribution_dir,
                project_file,
                settings,
            )

    # Overwrite the bin dir if it was explicitly set.
    active_locs = settings["active-config-locations"]
    if isinstance(active_locs, dict):
        active_locs["distribution-bin-dir"] = str(resolved_bin_dir)

    return settings


def reprocess_config(
    bin_dir: Path,
    dist_dir: Path,
    project_config_file: Path,
    orig: DictJsonData,
) -> DictJsonData:
    """Reprocess this configuration using the new settings."""
    # Save the configuration for reprocessing.
    tmp = save_to_tmp(orig)

    # Reprocess, now with the project file.
    try:
        return generate_config(
            bin_dir=bin_dir,
            distribution_dir=dist_dir,
            project_config_file=str(project_config_file),
            project_dir=None,
            dev_container_workspace_dir=None,
            dev_container_settings_dir=None,
            user_setup=[],
            universal_file=tmp,
            no_setup_search=True,
        )
    finally:
        os.unlink(tmp)


def save_to_tmp(data: DictJsonData) -> str:
    """Save the data to a temporary file."""
    fd, file = tempfile.mkstemp()
    try:
        os.write(fd, json.dumps(data).encode("utf-8"))
        os.close(fd)
        return file
    except:
        os.close(fd)
        os.unlink(file)
        raise


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


def find_bin_dir(bin_dir: str | None, dist_dir: Path) -> Path:
    """Find the bin/ directory."""
    if bin_dir:
        if not os.path.isdir(bin_dir):
            raise ConfigErr(f"Could not find bin directory '{bin_dir}'")
        return Path(bin_dir)
    return dist_dir / DISTRIBUTION_BIN_DIR


def _opt_resolved_path(data: DictJsonData, section: str, key: str) -> Path | None:
    """Get a resolved path."""
    loc = _opt_lookup(data, "resolved", section, "paths", key)
    if loc and isinstance(loc, str):
        return Path(loc)
    return None


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

def _opt_search_path(
    data: DictJsonData, section: str, key: str
) -> Path | None:
    """Search the path list at the section/key for the file.
    
    If the file is None, then this just tries to find the first of the path list that
    exists.  If the section/key does not exist, this raises a ConfigErr.
    """
    path_list = _opt_lookup(data, "resolved", section, "path-lists", key)
    if not isinstance(path_list, list):
        raise ConfigErr(f"Required {section}/path-lists/{key} in the configuration.")

    for loc in path_list:
        if not isinstance(loc, str):
            raise ConfigErr(f"Required {section}/path-lists/{key} in the configuration.")
        if os.path.isfile(loc):
            return Path(loc)
    return None


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

    config_file = save_to_tmp(config)

    py_exe = sys.executable

    # Change execution over the sub-command.
    cmd_args = [py_exe, str(cmd_file), config_file, *argv]
    print(cmd_args)
    os.execv(py_exe, cmd_args)  # nosec: B606  # We vetted the command and arguments.
    print(f"Failed to start sub-command {argv[0]} at {cmd_file}.", file=sys.stderr)
    return 127


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

    return run_sub_command(config, [sub_cmd, *sub_cmd_args], args.sub_cmd_dir)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
