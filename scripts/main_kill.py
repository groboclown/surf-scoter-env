#SPDX:MIT
"""Surf Scoter Sub-command: kill

Stops a dev environment whose `scoter run` is gone, or stuck: stops its
containers, fetches its project clone's changes into the project if asked,
removes its project clone or persistent store if asked, and forgets it (its
entry in the running environments file, and
`${scoter:state-dir}/environments/HOST`).
"""
import enum
import os
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from subprocess import SubprocessError

from surf_scoter_lib import (
    BAD_EXECUTION_ARGS_CODE,
    UserConfig,
    fileutil,
    gen_parser,
    persistent_store,
    ssh_hosts,
)
from surf_scoter_lib.active_env import ActiveEnvironmentSet, EnvironmentDesc
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.user_config import Capture, OnError

# Where scoter-host's copy of the configuration goes, under `scoter: paths: state-dir`
# (as `main_run.py` has it).
SUC_COPY_DIR = "environments"


class Clean(enum.Flag):
    """What `run_kill` removes once the environment stops."""

    NOTHING = 0
    # The project's clone (`--clean-project-clone`), unless it is, or holds,
    # something else.
    PROJECT_CLONE = enum.auto()
    # The persistent store volume (`--clean-persistent-store`).
    PERSISTENT_STORE = enum.auto()


class SshEntries(enum.Enum):
    """What `run_kill` does with the environment's ssh entries."""

    # Remove them as `ssh: values: config-cleanup` says.
    RELEASE = "release"
    # Leave them: `scoter cleanup` asks about them all at once.
    KEEP = "keep"


def run_kill(
    config: UserConfig,
    *,
    host: str | None,
    fetch_branch: str | None,
    clean: Clean = Clean.NOTHING,
    ssh: SshEntries = SshEntries.RELEASE,
) -> int:
    """Stop the environment `host`, or the project's one.

    With `SshEntries.RELEASE`, its ssh entries go as `ssh: values:
    config-cleanup` says; `scoter cleanup` keeps them, to ask about them all
    at once.
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
                "docker", "compose-down", on_error=OnError.RETURN, capture=Capture.NONE,
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
        print("Its project clone is kept, and it's still listed.", file=sys.stderr)
        return 1
    if Clean.PROJECT_CLONE in clean and env.project_clone_dir:
        problem = _unsafe_to_remove(Path(env.project_clone_dir), recorded)
        if problem:
            print(f"Not removing the project clone {env.project_clone_dir}: {problem}.", file=sys.stderr)
        else:
            print(f"Removing the project clone {env.project_clone_dir}...")
            fileutil.rm(Path(env.project_clone_dir))
    elif env.project_clone_dir:
        print(f"The project clone is kept: {env.project_clone_dir}")
    if Clean.PERSISTENT_STORE in clean:
        try:
            volume = persistent_store.volume_name(recorded)
        except SurfScoterErr as err:
            print(f"Not removing the persistent store: {err}", file=sys.stderr)
        else:
            if not persistent_store.remove(recorded, volume):
                print(f"Couldn't remove the persistent store {volume}.", file=sys.stderr)

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
    if ssh == SshEntries.RELEASE:
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
    """Fetch the project clone's `branch` into the project; False, having said why, if it can't be."""
    try:
        section = config.get_vcs_section()
    except SurfScoterErr as err:
        print(f"Can't fetch: {err}", file=sys.stderr)
        return False
    if section is None or not env.project_clone_dir or not env.project_dir:
        print("Can't fetch: the project has no version control system, or no clone.", file=sys.stderr)
        return False
    root = config.active_location("vcs-root") or Path(env.project_dir)
    print(f"Fetching {branch} from the project clone into {root}...")
    try:
        res = config.run_command(
            section, "fetch-changes-into", cwd=Path(root), on_error=OnError.RETURN,
            capture=Capture.NONE,
            template={"project-clone-dir": env.project_clone_dir, "branch": branch},
        )
    except (OSError, SubprocessError, SurfScoterErr) as err:
        print(f"Couldn't fetch: {err}", file=sys.stderr)
        return False
    if res.returncode != 0:
        print("Couldn't fetch the project clone's changes.", file=sys.stderr)
        return False
    print("Fetched.  Review them before you merge them (`show-fetch-changes`, for Git `git log -p HEAD..FETCH_HEAD`).")
    return True


def _unsafe_to_remove(clone: Path, config: UserConfig) -> str | None:
    """Why removing `clone` would remove more than a project clone, if it would."""
    real = Path(os.path.realpath(clone))
    if not real.is_dir() or os.path.islink(clone):
        return "it isn't a directory"
    guarded: list[tuple[str, Path | None]] = [
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
            "Fetch this branch from the project's clone into your project directory."
        ),
    )
    parser.add_argument(
        "--clean-project-clone", "--clean-project",
        action="store_true",
        default=False,
        dest="clean_project_clone",
        help=(
            "Once it's stopped, remove the project's clone.  Changes in it you haven't "
            "fetched are lost, so use it with '--fetch'.  The persistent store is kept."
        ),
    )
    parser.add_argument(
        "--clean-persistent-store",
        action="store_true",
        default=False,
        dest="clean_persistent_store",
        help="Once it's stopped, remove the dev container's persistent store.",
    )

    args = parser.parse_args(argv)
    return run_kill(
        config,
        host=args.host,
        fetch_branch=args.fetch_branch,
        clean=(
            (Clean.PROJECT_CLONE if args.clean_project_clone else Clean.NOTHING)
            | (Clean.PERSISTENT_STORE if args.clean_persistent_store else Clean.NOTHING)
        ),
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv))
