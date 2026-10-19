#SPDX:MIT
"""Surf Scoter Sub-command: user-setup"""
import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from surf_scoter_lib import (
    BAD_EXECUTION_ARGS_CODE,
    UserConfig,
    defaults,
    fileutil,
    gen_parser,
)

# ---------------------------------------------------------------------
# User-space setup.


def user_space_setup_runner(config: UserConfig, args: argparse.Namespace) -> int:
    """Run the user-space setup.
    
    This performs:
        1. key file construction, if it doesn't exist.
        2. Ensure the ssh config file exists.
    
    This doesn't construct the Host entry in the config file, because
    that can vary from container to container.
    """
    interactive = bool(args.interactive)
    gen_key_pair(config, interactive)
    ensure_ssh_config(config, interactive)
    ensure_ssh_known_hosts(config, interactive)
    try_user_home(config, interactive)
    return 0


def gen_key_pair(config: UserConfig, interactive: bool) -> None:
    # Step 1: generate the key pair.
    key_file = config.require_path("ssh", "key-file")
    pub_key_file = Path(str(key_file) + defaults.PUB_KEY_EXT)
    if key_file.is_file() and pub_key_file.is_file():
        answer = _read_user_input(
            f"The key file pair ({key_file}) already exists.  Replace? [yN]",
            interactive,
            "n",
        )
        if answer and answer[0] == "y":
            fileutil.rm(key_file)
            fileutil.rm(pub_key_file)
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
            fileutil.rm(key_file)
            fileutil.rm(pub_key_file)
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


def ensure_ssh_config(config: UserConfig, interactive: bool) -> None:
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
            fileutil.rm(ssh_config_file)

    if ssh_config_file.is_file() and not fileutil.is_windows():
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
            fileutil.touch(ssh_config_file, 0o600)
            print(f"Created an empty ssh config file ({ssh_config_file})")


def ensure_ssh_known_hosts(config: UserConfig, interactive: bool) -> None:
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
            fileutil.rm(known_hosts_file)
            if known_hosts_file.exists():
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
            fileutil.touch(known_hosts_file, fileutil.MODE_USER_RW)
    else:
        # Set the permissions without asking.
        fileutil.touch(known_hosts_file, fileutil.MODE_USER_RW)


def try_user_home(config: UserConfig, interactive: bool) -> None:
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
            fileutil.touch(user_setup, 0o644)


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


def main(argv: Sequence[str]) -> int:
    """Configure the CLI arguments."""
    config, argv, parser = gen_parser(
        argv,
        "Configure your home directory for use with Surf Scoter.",
    )
    if not config:
        return BAD_EXECUTION_ARGS_CODE
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
    args = parser.parse_args(argv)
    return user_space_setup_runner(config, args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
