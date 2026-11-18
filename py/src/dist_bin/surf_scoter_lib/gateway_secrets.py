#SPDX:MIT
"""Giving a running network gateway its secrets: shared by the gateways' post-start scripts.

A gateway that fills in secrets (seekrit, Envoy) gets them after it starts:
the setup's `dev-env: runs: network-gateway-post-start` runs its script
(`gateway_postrun_seekrit.py`, `gateway_postrun_envoy.py`) once the gateway
has started, and `scoter gateway-post-start` runs it again after the gateway
restarts.  This reads the secret names the egress rules give, and their
values from your secret store (the setup's `project-state: paths:
secret-store`), and sends them over `docker: runs: exec-stdin`: the
gateway's standard input, then a socket only its own user can reach.
They're held only in the gateway's memory: never in its configuration,
environment or files, nor on any command line.  Each gateway says how it
takes them with an `Injector`.

The secret store is a TOML file only you can read, outside the project:

    # The values, by name: each a command that prints it (run on this
    # computer, as you), or the value itself.
    [secrets]
    GITHUB_TOKEN = { command = ["gh", "auth", "token"] }
    OTHER_KEY = { value = "..." }

or, instead of `[secrets]`, a seekrit.dev service token, which gives the
values itself, for a gateway that can use one (`Injector.allow_token`):

    seekrit-token = { command = ["pass", "show", "seekrit/my-project"] }

Only the secrets the rules name are sent.  A name the rules give that the
store doesn't have stops it before anything is sent.  It never prints a value.

A container can take minutes after `compose up` before it runs, and then
before it takes commands: it waits for both, up to the setup's
`dev-env: values: network-gateway-start-seconds`.
"""
import datetime
import json
import os
import re
import shutil
import stat
import subprocess  # nosec: B404  # Runs the store's commands, as the user asked.
import sys
import time
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import gateway, setup_check, universal
from .err import SurfScoterErr
from .user_config import Capture, Echo, OnError, UserConfig

MAX_STORE_BYTES = 1024 * 1024
MAX_VALUE_BYTES = 64 * 1024
# How long a store command may take: long enough to type a passphrase.
COMMAND_TIMEOUT_SECONDS = 120
# How long to wait for the gateway to run, and take commands, when the setup
# doesn't say (`dev-env: values: network-gateway-start-seconds`).
START_SECONDS = 300.0
START_SECONDS_KEY = "network-gateway-start-seconds"
POLL_SECONDS = 2.0
# The gateway makes its socket as it starts: missing longer than this after
# the container started, it's gone, after a delivery.
SOCKET_WINDOW_SECONDS = 60.0
# A gateway's inject command's exit codes (`seekrit-proxy inject`'s, which
# `envoy-inject` shares): taken; refused; couldn't reach the socket.
INJECT_TAKEN = 0
INJECT_REFUSED = 1
INJECT_UNREACHABLE = 2
EXIT_FAILED = 1
EXIT_ERROR = 2

TOKEN_KEY = "seekrit-token"  # nosec: B105  # The store's key name, not a token.
SECRETS_KEY = "secrets"


class StoreErr(SurfScoterErr):
    """The secret store can't give what's needed."""


@dataclass(frozen=True)
class Injector:
    """How a gateway takes its secrets.

    `command`: what to run in the gateway's container (with `docker: runs:
    exec-stdin`), which reads the JSON payload on its standard input and
    exits with one of the `INJECT_*` codes.  `allow_token`: whether it can
    take a seekrit.dev token in place of the values.  `socket_closes`:
    whether the gateway removes its socket after a delivery, so a socket
    missing well after it started means it already has one.
    `skip_without_names`: whether a gateway whose rules name no secrets is
    left alone (it has no socket then), rather than sent an empty set.
    """

    command: tuple[str, ...]
    allow_token: bool
    socket_closes: bool
    skip_without_names: bool


# ---------------------------------------------------------------------
# The secret store


def secret_names(config: UserConfig) -> list[str]:
    """The secrets the configuration's egress rules name, each once, in order."""
    names: list[str] = []
    for rule in gateway.rules_from(config):
        names.extend(name for name in rule.secrets if name not in names)
    return names


def read_store(config: UserConfig, path: Path) -> dict[str, object]:
    """The secret store at `path`, checked: yours, only yours to read, and outside the project."""
    where = setup_check.inside_project(config, path)
    if where:
        raise StoreErr(f"the secret store {path} is in {where}; keep it outside the project")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError as err:
        raise StoreErr(f"there's no secret store at {path}") from err
    except OSError as err:
        raise StoreErr(f"can't read the secret store {path} (a symlink isn't followed): {err}") from err
    with os.fdopen(fd, "rb") as inp:
        info = os.fstat(inp.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise StoreErr(f"the secret store {path} isn't a file")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise StoreErr(f"the secret store {path} isn't yours")
        if info.st_mode & 0o077:
            raise StoreErr(
                f"others can use the secret store {path} (mode {stat.S_IMODE(info.st_mode):o}); "
                "make it readable only by you (chmod 600)"
            )
        data = inp.read(MAX_STORE_BYTES + 1)
    if len(data) > MAX_STORE_BYTES:
        raise StoreErr(f"the secret store {path} is larger than {MAX_STORE_BYTES} bytes")
    try:
        store = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        # The parser's message can quote the file; give only where.
        line = getattr(err, "lineno", None)
        raise StoreErr(f"the secret store {path} isn't TOML" + (f" (line {line})" if line else "")) from None
    unknown = sorted(set(store) - {TOKEN_KEY, SECRETS_KEY})
    if unknown:
        raise StoreErr(f"the secret store {path} has {', '.join(unknown)}; it can only have `{TOKEN_KEY}` or `[{SECRETS_KEY}]`")
    if TOKEN_KEY in store and SECRETS_KEY in store:
        raise StoreErr(f"the secret store {path} has both `{TOKEN_KEY}` and `[{SECRETS_KEY}]`; give one")
    return store


def _program(config: UserConfig, name: str, what: str) -> str:
    """A store command's program, found as the tools find programs: never in the project."""
    if os.path.isabs(name):
        found: str | None = name
    else:
        if os.sep in name or (os.altsep and os.altsep in name):
            raise StoreErr(f"{what}: give the program as a name or an absolute path, not {name!r}")
        # Only absolute PATH entries: a relative one is wherever this runs.
        path = os.pathsep.join(
            entry for entry in os.environ.get("PATH", "").split(os.pathsep) if os.path.isabs(entry)
        )
        found = shutil.which(name, path=path)
    if not found or not os.access(found, os.X_OK):
        raise StoreErr(f"{what}: can't find the program {name!r}")
    where = setup_check.inside_project(config, Path(found))
    if where:
        raise StoreErr(f"{what}: the program {found} is in {where}; it would run the project's code")
    return found


def entry_value(config: UserConfig, entry: object, what: str) -> str:
    """A store entry's value: `{value = "..."}`, or `{command = [...]}`'s output."""
    if not isinstance(entry, dict) or len(entry) != 1 or not ({"value", "command"} & set(entry)):
        raise StoreErr(f"{what} must be `{{ value = \"...\" }}` or `{{ command = [\"program\", ...] }}`")
    if "value" in entry:
        value = entry["value"]
        if not isinstance(value, str):
            raise StoreErr(f"{what}: `value` must be text")
    else:
        command = entry["command"]
        if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command):
            raise StoreErr(f"{what}: `command` must be a list of text, starting with the program")
        program = _program(config, command[0], what)
        try:
            # Its errors, and any prompt, go to you; its output is the value.
            res = subprocess.run(  # nosec: B603  # The user's own command, from their own store.
                [program, *command[1:]],
                stdout=subprocess.PIPE, check=False, timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            raise StoreErr(f"{what}: its command didn't run: {err}") from None
        if res.returncode != 0:
            raise StoreErr(f"{what}: its command failed (exit {res.returncode})")
        if len(res.stdout) > MAX_VALUE_BYTES:
            raise StoreErr(f"{what}: its command printed more than {MAX_VALUE_BYTES} bytes")
        try:
            value = res.stdout.decode("utf-8")
        except UnicodeDecodeError:
            raise StoreErr(f"{what}: its command didn't print UTF-8 text") from None
        # A command ends its line; the value doesn't.
        value = value.removesuffix("\n").removesuffix("\r")
    if not value:
        raise StoreErr(f"{what} is empty")
    return value


def payload(config: UserConfig, names: list[str], allow_token: bool) -> bytes:
    """What the gateway's `inject` reads: the token, or the named values.

    With no names, an empty set of values: the gateway's socket closes, and
    it has nothing to give.  Without `allow_token` (a gateway that can't
    resolve one, such as Envoy), a store with a token is refused.
    """
    if not names:
        return json.dumps({"values": {}}).encode("utf-8")
    path = config.optional_path("project-state", "secret-store")
    if path is None:
        raise StoreErr(
            f"the egress rules name {', '.join(names)}, but the setup has no "
            "`project-state: paths: secret-store`"
        )
    store = read_store(config, path)
    if TOKEN_KEY in store and not allow_token:
        raise StoreErr(
            f"the secret store {path} has `{TOKEN_KEY}`, which this gateway can't use; "
            f"give the values in `[{SECRETS_KEY}]`"
        )
    if TOKEN_KEY in store:
        return json.dumps({"token": entry_value(config, store[TOKEN_KEY], f"`{TOKEN_KEY}`")}).encode("utf-8")
    secrets = store.get(SECRETS_KEY, {})
    if not isinstance(secrets, dict):
        raise StoreErr(f"`[{SECRETS_KEY}]` in the secret store {path} must be a table")
    missing = [name for name in names if name not in secrets]
    if missing:
        raise StoreErr(
            f"the egress rules name {', '.join(missing)}, which the secret store {path} doesn't have"
        )
    values = {name: entry_value(config, secrets[name], f"`{name}` in the secret store") for name in names}
    return json.dumps({"values": values}).encode("utf-8")


# ---------------------------------------------------------------------
# The command


def start_seconds(config: UserConfig) -> float:
    """How long to wait for the gateway: the setup's, or `START_SECONDS`."""
    try:
        seconds = float(config.require_value("dev-env", START_SECONDS_KEY))
    except SurfScoterErr:
        return START_SECONDS
    except ValueError as err:
        raise SurfScoterErr(f"the setup's `dev-env: values: {START_SECONDS_KEY}` isn't a number") from err
    return seconds if seconds > 0 else START_SECONDS


_FRACTION = re.compile(r"(\.\d{6})\d+")


def started_at(text: str) -> datetime.datetime | None:
    """A container's `State.StartedAt` (Docker: `...123456789Z`; Podman: `...123456789-06:00`)."""
    try:
        when = datetime.datetime.fromisoformat(_FRACTION.sub(r"\1", text.strip()))
    except ValueError:
        return None
    return when if when.tzinfo else None


def container_state(config: UserConfig, container: str) -> tuple[str, datetime.datetime | None]:
    """The container's status (`running`, `exited`, ...; `unknown` when it can't be
    inspected yet), and when it started."""
    res = config.run_command(
        "docker", "inspect-container",
        template={"name": container},
        capture=Capture.BOTH,
        on_error=OnError.RETURN,
        echo=Echo.QUIET,
    )
    if res.returncode != 0:
        return "unknown", None
    try:
        data = json.loads(res.stdout)
        if isinstance(data, list) and data:
            data = data[0]
        state = data.get("State") if isinstance(data, dict) else None
    except ValueError:
        return "unknown", None
    if not isinstance(state, dict):
        return "unknown", None
    if state.get("Running") is True:
        status = "running"
    else:
        status = str(state.get("Status") or "unknown").lower()
    when = state.get("StartedAt")
    return status, started_at(when) if isinstance(when, str) else None


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _show(res: "subprocess.CompletedProcess[bytes]") -> None:
    """What `inject` (or the container tool) said, on its last try: never a value."""
    for stream, out in ((res.stdout, sys.stdout), (res.stderr, sys.stderr)):
        text = (stream or b"").decode("utf-8", "replace").strip()
        if text:
            print(text, file=out)


def inject(config: UserConfig, container: str, data: bytes, injector: Injector) -> bool:
    """Send `data` to the gateway's inject command once it runs and takes commands;
    whether it took it."""
    wait = start_seconds(config)
    deadline = time.monotonic() + wait
    said = False
    last: subprocess.CompletedProcess[bytes] | None = None
    while True:
        status, started = container_state(config, container)
        if status in ("exited", "dead", "stopped"):
            print(f"The network gateway ({container}) isn't running ({status}).", file=sys.stderr)
            return False
        if status == "running":
            res = config.run_command(
                "docker", "exec-stdin",
                template={"container": container},
                extra_args=list(injector.command),
                stdin_data=data,
                capture=Capture.BOTH,
                on_error=OnError.RETURN,
                echo=Echo.QUIET,
            )
            last = res
            if res.returncode in (INJECT_TAKEN, INJECT_REFUSED):
                _show(res)
                return res.returncode == INJECT_TAKEN
            age = None if started is None else (_now() - started).total_seconds()
            if (
                injector.socket_closes and res.returncode == INJECT_UNREACHABLE
                and age is not None and age > SOCKET_WINDOW_SECONDS
            ):
                _show(res)
                print(
                    f"The network gateway has run for {int(age)} seconds without its socket: it already "
                    "has its secrets (it takes one delivery), or it didn't start right.  Restart it "
                    "to give them again.",
                    file=sys.stderr,
                )
                return False
            # Otherwise its socket isn't there yet, or the container doesn't
            # take commands yet.
        if time.monotonic() >= deadline:
            if last is not None:
                _show(last)
            print(
                f"The network gateway ({container}) didn't take its secrets within "
                f"{wait:g} seconds (`dev-env: values: {START_SECONDS_KEY}`).",
                file=sys.stderr,
            )
            return False
        if not said:
            print("Waiting for the network gateway to start...")
            said = True
        time.sleep(POLL_SECONDS)


def run(argv: Sequence[str], injector: Injector) -> int:
    """A post-start script's whole run, `argv` being `SCRIPT UNIVERSAL.json
    CONTAINER`: give the gateway in CONTAINER the secrets the egress rules
    name, from the secret store, with `injector`.  Its exit code."""
    if len(argv) != 3:
        print(f"Usage: {argv[0] if argv else 'gateway_postrun.py'} UNIVERSAL.json CONTAINER", file=sys.stderr)
        return EXIT_ERROR
    try:
        config = universal.read_universal(argv[1])
        names = secret_names(config)
    except (ValueError, SurfScoterErr) as err:  # A bad rule is a `ValueError` (`gateway.RuleErr`).
        print(f"{argv[0]}: {err}", file=sys.stderr)
        return EXIT_ERROR
    if injector.skip_without_names and not names:
        return 0
    try:
        data = payload(config, names, injector.allow_token)
    except (OSError, SurfScoterErr) as err:
        print(f"Can't give the network gateway its secrets: {err}", file=sys.stderr)
        return EXIT_FAILED
    print(f"Giving the network gateway {len(names)} secret(s): {', '.join(names) or 'none'}")
    if not inject(config, argv[2], data, injector):
        print("The network gateway didn't take its secrets.", file=sys.stderr)
        return EXIT_FAILED
    return 0
