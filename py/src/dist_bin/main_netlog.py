#SPDX:MIT
"""Show what a running dev environment did through its network gateway.

A Surf Scoter sub-command.

Reads the gateway container's log (`docker: runs: image-logs`), and shows
the hosts the dev container reached or was refused, the requests a gateway
that reads them saw, and the secrets put into them (by name).  For each
refused host it suggests the egress rule that would allow it; it never
changes the configuration.  `--follow` shows each event as it happens: with
the setup's `docker: runs: logs-follow` when it has one, or else by reading
the log again every few seconds (some container tools, such as Podman on
WSL2, can't follow a log).
"""
import json
import subprocess  # nosec: B404  # The setup's own log command.
import sys
import time
from collections.abc import Iterable, Sequence

from main_kill import choose_environment
from surf_scoter_lib import BAD_EXECUTION_ARGS_CODE, UserConfig, gen_parser, netlog
from surf_scoter_lib.active_env import ActiveEnvironmentSet
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.user_config import Capture, Echo, OnError

POLL_SECONDS = 3.0


def _log_lines(config: UserConfig, container: str) -> list[str] | None:
    """The gateway's whole log, both streams; None when it can't be read."""
    res = config.run_command(
        "docker", "image-logs", template={"name": container},
        capture=Capture.BOTH, on_error=OnError.RETURN, echo=Echo.QUIET,
    )
    if res.returncode != 0:
        return None
    text = (res.stdout or b"") + b"\n" + (res.stderr or b"")
    return text.decode("utf-8", "replace").splitlines()


def _show(events: Iterable[netlog.Event], as_json: bool) -> None:
    for event in events:
        print(json.dumps(event.to_json()) if as_json else event.line(), flush=True)


def _follow_stream(config: UserConfig, container: str, parser: netlog.Parser, as_json: bool) -> int:
    """Follow with the setup's `logs-follow`, until it ends or you stop it."""
    cmd = config.require_run("docker", "logs-follow", {"name": container})
    with subprocess.Popen(  # nosec: B603  # The setup's own command.
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=config.command_env("docker"),
    ) as proc:
        for raw in proc.stdout or ():
            event = parser(raw.decode("utf-8", "replace"))
            if event is not None:
                _show([event], as_json)
    return proc.returncode or 0


def _follow_poll(config: UserConfig, container: str, parser: netlog.Parser, as_json: bool) -> int:
    """Follow by reading the log again and again, showing only what's new.

    New is a line the last read didn't have, in the log's order.  Its lines
    carry their time (to the microsecond, for seekrit), so they don't repeat;
    the log may be cut short in the meantime (`logging-max-size`), but only
    of lines already shown.
    """
    before: set[str] = set()
    while True:
        lines = _log_lines(config, container)
        if lines is None:
            print(f"Can't read the gateway's log ({container}); has the environment stopped?", file=sys.stderr)
            return 1
        _show(
            (event for event in map(parser, (line for line in lines if line not in before)) if event is not None),
            as_json,
        )
        before = set(lines)
        time.sleep(POLL_SECONDS)


def run_netlog(config: UserConfig, host: str | None, *, follow: bool, as_json: bool) -> int:
    """Show the environment `host`'s (or the project's) gateway log."""
    try:
        file = config.require_path("scoter", "running-envs")
        env = choose_environment(config, ActiveEnvironmentSet.read(file), host)
    except SurfScoterErr as err:
        print(err, file=sys.stderr)
        return 1
    if env is None:
        return 1
    if not env.suc:
        print(f"{env.host} is only reserved: it has no gateway yet.", file=sys.stderr)
        return 1
    # The gateway, and the commands, the environment runs with.
    recorded = UserConfig(env.suc)
    name = recorded.require_value("dev-env", "network-gateway-name")
    parser = netlog.PARSERS.get(name)
    if parser is None:
        print(f"There's no reader for the `{name}` gateway's log; it reads {', '.join(sorted(netlog.PARSERS))}.", file=sys.stderr)
        return 1
    container = env.network_gateway_name
    try:
        if follow:
            if recorded.has_run("docker", "logs-follow"):
                return _follow_stream(recorded, container, parser, as_json)
            return _follow_poll(recorded, container, parser, as_json)
    except KeyboardInterrupt:
        return 0
    lines = _log_lines(recorded, container)
    if lines is None:
        print(f"Can't read the gateway's log ({container}).", file=sys.stderr)
        return 1
    events = netlog.parse(lines, parser)
    if as_json:
        print(json.dumps([event.to_json() for event in events], indent=2))
    else:
        print(f"{env.host}: the {name} gateway's log, {len(events)} events.")
        for line in netlog.summarize(events).lines():
            print(line)
    return 0


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "Show what the project's dev environment (or another, with --host) did through "
        "its network gateway: the hosts it reached and was refused, the requests the "
        "gateway read, and the secrets it put in them (by name).  For a refused host, "
        "it suggests the egress rule that would allow it."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE
    parser.add_argument(
        "--host", action="store", dest="host",
        help="The environment, if it isn't the project's only one.  Doesn't need the project.",
    )
    parser.add_argument(
        "-f", "--follow", action="store_true", default=False, dest="follow",
        help="Show each event as it happens, until you stop it (ctrl-c).",
    )
    parser.add_argument(
        "--json", action="store_true", default=False, dest="as_json",
        help="Each event as JSON: a list, or with --follow, one a line.",
    )
    args = parser.parse_args(argv)
    return run_netlog(config, args.host, follow=args.follow, as_json=args.as_json)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
