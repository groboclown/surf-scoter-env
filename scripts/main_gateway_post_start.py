#SPDX:MIT
"""Run a running environment's network gateway post-start again.

A Surf Scoter sub-command.

`scoter run` runs the setup's `dev-env: runs: network-gateway-post-start`
once the gateway starts.  What it gives (the seekrit gateway's secrets) is
held only in the gateway's memory, so a gateway that restarted has lost it:
this gives it again, with the configuration the environment runs.
"""
import sys
from collections.abc import Sequence

from main_kill import SUC_COPY_DIR, choose_environment
from surf_scoter_lib import (
    BAD_EXECUTION_ARGS_CODE,
    UserConfig,
    gateway_start,
    gen_parser,
)
from surf_scoter_lib.active_env import ActiveEnvironmentSet
from surf_scoter_lib.err import SurfScoterErr

# scoter-host's copy of the configuration, in `SUC_COPY_DIR/HOST` (as `main_run.py` has it).
SUC_COPY_FILE = "suc.json"


def run_post_start(config: UserConfig, host: str | None) -> int:
    """Run the post-start for the environment `host`, or the project's one."""
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
    # The configuration the environment runs, as scoter-host has it.
    recorded = UserConfig(env.suc)
    state_dir = recorded.optional_path("scoter", "state-dir")
    universal = None if state_dir is None else state_dir / SUC_COPY_DIR / env.host / SUC_COPY_FILE
    if universal is None or not universal.is_file():
        print(f"{env.host}'s configuration is gone; is its `scoter run` still running?", file=sys.stderr)
        return 1
    if not recorded.has_run("dev-env", gateway_start.POST_START):
        print(f"{env.host}'s gateway has no post-start; there's nothing to do.")
        return 0
    if not gateway_start.post_start(recorded, universal, env.network_gateway_name):
        print("The network gateway's post-start failed.", file=sys.stderr)
        return 1
    print(f"Ran {env.host}'s network gateway post-start.")
    return 0


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "Run the project's dev environment's network gateway post-start again (or "
        "another environment's, with --host): after the gateway restarts, it gives "
        "the gateway what it holds only in memory, such as its secrets."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE
    parser.add_argument(
        "--host",
        action="store",
        dest="host",
        help="The environment, if it isn't the project's only one.  Doesn't need the project.",
    )
    args = parser.parse_args(argv)
    return run_post_start(config, args.host)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
