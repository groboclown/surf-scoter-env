#SPDX:MIT
"""Surf Scoter Sub-command: kill

Stops a dev environment whose `scoter run` is gone, or stuck: stops its
containers, fetches its workspace's changes into the project if asked,
removes its workspace if asked, and forgets it (its entry in the running
environments file, and `${scoter:state-dir}/environments/HOST`).
"""
import os
import subprocess  # nosec: B404  # Runs the setup's own commands.
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from surf_scoter_lib import (
    BAD_EXECUTION_ARGS_CODE,
    UserConfig,
    fileutil,
    gen_parser,
    ssh_hosts,
)
from surf_scoter_lib.active_env import ActiveEnvironmentSet, EnvironmentDesc
from surf_scoter_lib.err import SurfScoterErr

# Where scoter-host's copy of the configuration goes, under `scoter: paths: state-dir`
# (as `main_run.py` has it).
SUC_COPY_DIR = "environments"


def run_kill(
    config: UserConfig,
    *,
    host: str | None,
    fetch_branch: str | None,
    force_clean: bool,
    release_ssh: bool = True,
) -> int:
    """Stop the environment `host`, or the project's one.

    With `release_ssh`, its ssh entries go as `ssh: values: config-cleanup`
    says; `scoter cleanup` leaves them out, to ask about them all at once.
    """
    try:
        file = config.require_path("scoter", "running-envs")
        lock_file = config.require_path("scoter", "running-envs-lock")
        env = _choose(config, ActiveEnvironmentSet.read(file), host)
    except SurfScoterErr as err:
        print(err, file=sys.stderr)
        return 1
    if env is None:
        return 1
    # The configuration it ran with, so it's stopped as it was started.  A
    # reservation (the run stopped before it built anything) has none.
    recorded = UserConfig(env.suc) if env.suc else config
    print(f"Stopping {env.host}...")
    if env.compose_file and env.compose_env_file:
        if not (Path(env.compose_file).is_file() and Path(env.compose_env_file).is_file()):
            print("Its compose files are gone; there are no containers to stop.", file=sys.stderr)
        else:
            res = recorded.run_command(
                "docker", "compose-down", check=False, capture_stdout=False,
                template={"env-file": env.compose_env_file, "compose-file": env.compose_file},
            )
            if res.returncode != 0:
                print(
                    f"Couldn't stop its containers; it's still listed.  Stop them by hand "
                    f"(`{env.dev_env_name}`, `{env.firewall_router_name}`, `{env.network_gateway_name}`), "
                    "then run this again.",
                    file=sys.stderr,
                )
                return 1
    else:
        print("It was only reserved: its run stopped before it started any containers.")

    if fetch_branch and not _fetch(recorded, env, fetch_branch):
        print("Its workspace is kept, and it's still listed.", file=sys.stderr)
        return 1
    if force_clean and env.workspace_dir:
        problem = _unsafe_to_remove(Path(env.workspace_dir), recorded)
        if problem:
            print(f"Not removing the workspace {env.workspace_dir}: {problem}.", file=sys.stderr)
        else:
            print(f"Removing the workspace {env.workspace_dir}...")
            fileutil.rm(Path(env.workspace_dir))
    elif env.workspace_dir:
        print(f"The workspace is kept: {env.workspace_dir}")

    try:
        with ActiveEnvironmentSet.locked(file, lock_file) as envs:
            envs.rm_environ_by_host(env.host)
        state_dir = recorded.optional_path("scoter", "state-dir")
        if state_dir is not None:
            fileutil.rm(state_dir / SUC_COPY_DIR / env.host)
    except (OSError, SurfScoterErr) as err:
        print(f"Couldn't forget {env.host}: {err}", file=sys.stderr)
        return 1
    print(f"Stopped {env.host}.")
    if release_ssh:
        try:
            if ssh_hosts.release(config, env.host):
                print(f"Removed the ssh entries for {env.host}.")
        except (OSError, SurfScoterErr) as err:
            print(f"Couldn't remove the ssh entries for {env.host}: {err}", file=sys.stderr)
    return 0


def _choose(
    config: UserConfig, envs: ActiveEnvironmentSet, host: str | None,
) -> EnvironmentDesc | None:
    """The environment to stop: `host`, or the project's only one; None, having said why."""
    if host:
        env = envs.get_environ_by_host(host)
        if env is None:
            print(f"No environment `{host}` is running.", file=sys.stderr)
            _list(envs.environs.values())
        return env
    project = config.active_location("project-dir")
    if project is None:
        print("There's no project here: give the environment with --host.", file=sys.stderr)
        _list(envs.environs.values())
        return None
    found = envs.environs_for_project(str(project))
    if len(found) == 1:
        return found[0]
    if not found:
        print("No environment is running for this project.", file=sys.stderr)
        _list(envs.environs.values())
    else:
        print("More than one environment is running for this project: give one with --host.", file=sys.stderr)
        _list(found)
    return None


def _list(envs: Iterable[EnvironmentDesc]) -> None:
    listed = list(envs)
    if listed:
        print("Running environments:", file=sys.stderr)
    for env in listed:
        what = "running" if env.suc else "reserved"
        print(f"  {env.host}  {what}  {env.project_dir or '-'}  since {env.created_at}", file=sys.stderr)


def _fetch(config: UserConfig, env: EnvironmentDesc, branch: str) -> bool:
    """Fetch the workspace's `branch` into the project; False, having said why, if it can't be."""
    try:
        section = config.get_vcs_section()
    except SurfScoterErr as err:
        print(f"Can't fetch: {err}", file=sys.stderr)
        return False
    if section is None or not env.workspace_dir or not env.project_dir:
        print("Can't fetch: the project has no version control system, or no workspace.", file=sys.stderr)
        return False
    root = config.active_location("vcs-root") or Path(env.project_dir)
    print(f"Fetching {branch} from the workspace into {root}...")
    try:
        res = config.run_command(
            section, "fetch-changes-into", cwd=Path(root), check=False, capture_stdout=False,
            template={"workspace-dir": env.workspace_dir, "branch": branch},
        )
    except (OSError, subprocess.SubprocessError, SurfScoterErr) as err:
        print(f"Couldn't fetch: {err}", file=sys.stderr)
        return False
    if res.returncode != 0:
        print("Couldn't fetch the workspace's changes.", file=sys.stderr)
        return False
    print("Fetched.  Review them before you merge them (`show-fetch-changes`, for Git `git log -p HEAD..FETCH_HEAD`).")
    return True


def _unsafe_to_remove(workspace: Path, config: UserConfig) -> str | None:
    """Why removing `workspace` would remove more than a workspace, if it would."""
    real = Path(os.path.realpath(workspace))
    if not real.is_dir() or os.path.islink(workspace):
        return "it isn't a directory"
    guarded = [
        ("the project directory", config.active_location("project-dir")),
        ("the repository", config.active_location("vcs-root")),
        ("your home directory", Path.home()),
    ]
    for name, path in guarded:
        if path is None:
            continue
        other = Path(os.path.realpath(path))
        if other == real or other.is_relative_to(real):
            return f"it is, or holds, {name}"
    return None


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "Stop the project's dev environment, or another (--host).  `scoter run` stops "
        "its environment when it ends, but a crash, or a run stopped some other way, can "
        "leave one running: this stops its containers and forgets it."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE

    # Add arguments to the parser
    parser.add_argument(
        "--host",
        action="store",
        dest="host",
        help="Force a specific host to stop.  Doesn't need the project.",
    )
    parser.add_argument(
        "-f",
        "--fetch",
        action="store",
        dest="fetch_branch",
        help=(
            "Fetch the files from the workspace into your project directory into this branch."
        ),
    )
    parser.add_argument(
        "--force-clean",
        action="store_true",
        default=False,
        dest="force_clean",
        help=(
            "Force delete of the workspace files.  Setting this can potentially"
            "delete changes, so you should use this with the '--fetch' argument."
        ),
    )

    args = parser.parse_args(argv)
    return run_kill(
        config,
        host=args.host,
        fetch_branch=args.fetch_branch,
        force_clean=args.force_clean,
    )

if __name__ == "__main__":
    sys.exit(main(sys.argv))
