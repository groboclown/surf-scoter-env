#SPDX:MIT
"""Each environment's ssh entries: its `Host` block in the ssh config, and its known host key.

An environment is named `${ssh:host-prefix}${scoter:project-id}` (its dev
container, and its ssh `Host`), so tools that offer the hosts in
`~/.ssh/config` (VS Code's Remote-SSH) offer it by name (ticket #12).  Each
run writes its own block, marked as the tools': a block they didn't write
is never changed or removed.

When an environment goes, `ssh: values: config-cleanup` says what happens
to its entries: `no` keeps them, `auto` removes them, `ask` (the default)
asks at a terminal, and keeps them without one.  `scoter cleanup` finds the
marked blocks no running environment has.
"""
import re
import shlex
import sys
from collections.abc import Callable, Iterable

from . import argutil, fileutil
from .err import ConfigErr
from .ssh_config import SshConfig
from .user_config import Capture, Echo, OnError, UserConfig

# The first line in each block the tools write: how they tell theirs apart.
MARKER = "# Surf Scoter environment"
# `ssh: values: config-cleanup`: keep an environment's entries, remove them,
# or ask at a terminal.
CLEANUP_NO = "no"
CLEANUP_AUTO = "auto"
CLEANUP_ASK = "ask"
CLEANUP_MODES = (CLEANUP_NO, CLEANUP_AUTO, CLEANUP_ASK)
# What a host name may hold: it names containers and images, and is an ssh `Host`.
_HOST = re.compile(r"[a-z0-9][a-z0-9.-]{0,62}")
# A user name, as the container's `dev-env.yaml` gives it: it goes into the
# ssh config, so nothing that could end the line or start another (DEV-1).
_USER = re.compile(r"[A-Za-z_][A-Za-z0-9._-]{0,31}")


def host_name(config: UserConfig) -> str:
    """The project's environment's host: `${ssh:host-prefix}${scoter:project-id}`."""
    project_id = config.raw("active-config-locations", "project-id")
    if not isinstance(project_id, str) or not project_id:
        raise ConfigErr("the universal configuration file has no `project-id`")
    host = config.require_value("ssh", "host-prefix") + project_id
    if not _HOST.fullmatch(host):
        raise ConfigErr(
            f"`{host}` can't name an environment: `ssh: values: host-prefix` must be lowercase "
            "letters, digits, `.` and `-`, and the whole name at most 63 characters"
        )
    return host


def cleanup_mode(config: UserConfig) -> str:
    """`ssh: values: config-cleanup`: `no`, `auto` or `ask` (the default)."""
    raw = config.raw("resolved", "ssh", "values", "config-cleanup")
    # YAML reads a bare `no` as false.
    value = CLEANUP_NO if raw is False else CLEANUP_ASK if raw is None else str(raw)
    if value not in CLEANUP_MODES:
        raise ConfigErr(f"`ssh: values: config-cleanup` must be `no`, `auto` or `ask`, not `{value}`")
    return value


def is_ours(ssh_config: SshConfig, host: str) -> bool:
    """Whether the `Host` block is one the tools wrote (it has the marker)."""
    return any(line.line.strip().startswith(MARKER) for line in ssh_config.by_host.get(host, []))


def _read(config: UserConfig) -> SshConfig:
    ssh_config = SshConfig.read_ssh_config(config)
    if ssh_config is None:
        raise ConfigErr("your ssh config can't be used; run `scoter user-setup`")
    return ssh_config


def _save(config: UserConfig, ssh_config: SshConfig) -> None:
    file = config.require_path("ssh", "config-file")
    fileutil.backup(file, config.require_value("scoter", "backup-file-extension"))
    file.write_text(ssh_config.output(), encoding="utf-8")
    # Ensure the file mode wasn't changed.
    fileutil.touch(file, fileutil.MODE_USER_RW)


def write_host(config: UserConfig, host: str, user: str, port: str) -> None:
    """Write the environment's `Host` block, in place of one the tools wrote before.

    A block of that name the tools didn't write is the user's: it's refused.
    So is a `user` (from the project's `dev-env.yaml`) that isn't a plain
    user name, and a port that isn't a number.  Hold the running
    environments' lock, so two runs don't write at once.
    """
    if not _USER.fullmatch(user):
        raise ConfigErr(
            f"the dev container's `user` ({user!r}) isn't a user name: letters, digits, `.`, `_` and `-`"
        )
    if not port.isdigit():
        raise ConfigErr(f"the dev container's sshd port ({port!r}) isn't a number")
    ssh_config = _read(config)
    if ssh_config.has_host(host):
        if not is_ours(ssh_config, host):
            raise ConfigErr(
                f"your ssh config ({config.require_path('ssh', 'config-file')}) has a `Host {host}` "
                "the tools didn't write; rename or remove it, so they can write their own"
            )
        ssh_config.remove_host(host)
    proxy = config.require_run("ssh", "proxy-command", template={"port": port})
    ssh_config.add_host(host)
    ssh_config.add_comment(host, f"  {MARKER}; see `ssh: values: config-cleanup`.")
    ssh_config.add_key_value(host, "User", user)
    ssh_config.add_key_value(host, "IdentityFile", str(config.require_path("ssh", "key-file")))
    ssh_config.add_key_value(host, "IdentitiesOnly", "yes")
    ssh_config.add_key_value(host, "StrictHostKeyChecking", "yes")
    ssh_config.add_key_value(host, "ProxyCommand", shlex.join(proxy))
    ssh_config.add_blank(host)
    _save(config, ssh_config)


def leftover_hosts(config: UserConfig, running: Iterable[str]) -> list[str]:
    """The `Host` blocks the tools wrote whose environment isn't in `running`."""
    ssh_config = _read(config)
    prefix = config.require_value("ssh", "host-prefix")
    keep = set(running)
    return sorted(
        host for host in ssh_config.by_host
        if host and host.startswith(prefix) and host not in keep and is_ours(ssh_config, host)
    )


def remove_hosts(config: UserConfig, hosts: Iterable[str]) -> list[str]:
    """Remove the hosts' blocks the tools wrote, and their known host keys; the hosts removed."""
    ssh_config = _read(config)
    removed = [host for host in hosts if ssh_config.has_host(host) and is_ours(ssh_config, host)]
    for host in removed:
        ssh_config.remove_host(host)
    if removed:
        _save(config, ssh_config)
    for host in removed:
        config.run_command(
            "ssh", "remove-known-host", on_error=OnError.RETURN, echo=Echo.QUIET,
            capture=Capture.BOTH, template={"host": host},
        )
    return removed


def release(
    config: UserConfig, host: str, *, ask: Callable[[str], str] | None = None,
) -> bool:
    """The environment `host` went: remove its ssh entries as `config-cleanup` says; whether they went.

    `ask` asks the user (by default, at the terminal, when there is one).
    """
    mode = cleanup_mode(config)
    if mode == CLEANUP_NO:
        return False
    if mode == CLEANUP_ASK:
        if ask is None:
            if not argutil.can_ask():
                print(
                    f"Kept the ssh entries for {host}; `scoter cleanup` can remove them later.",
                    file=sys.stderr,
                )
                return False
            ask = input
        try:
            answer = ask(f"Remove the ssh config entry for {host}? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = ""
        if answer not in ("y", "yes"):
            return False
    return bool(remove_hosts(config, [host]))
