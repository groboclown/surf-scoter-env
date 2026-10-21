#SPDX:MIT
"""Surf Scoter Sub-command: cleanup

Finds what Surf Scoter left behind that nothing else removes, lists it,
and removes it once you say so: environments whose `scoter run` is gone
(stopped as `scoter kill` does), a run's state no record uses, the
accepted settings of projects whose directory is gone, and the verified
store's leftovers.  With `--workspaces`, the workspaces no running
environment uses; with `--images`, the images it built that none uses.
See step 23 of docs/contributors/projects-v1.md, and docs/cleaning-up.md.
"""
import datetime
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import main_kill
from surf_scoter_lib import (
    BAD_EXECUTION_ARGS_CODE,
    UserConfig,
    cleanup,
    gen_parser,
    review,
)
from surf_scoter_lib.active_env import EnvironmentDesc
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.verified import revoke, store

PROMPT = "Remove these? [y/N]: "


@dataclass
class Found:
    """What the cleanup found."""

    stale: list[EnvironmentDesc] = field(default_factory=list)
    state: list[Path] = field(default_factory=list)
    gone: list[revoke.Environment] = field(default_factory=list)
    leftovers: cleanup.StoreLeftovers = field(default_factory=cleanup.StoreLeftovers)
    workspaces: list[Path] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    verified_store: store.VerifiedStore | None = None

    def __bool__(self) -> bool:
        return bool(
            self.stale or self.state or self.gone or self.leftovers or self.workspaces or self.images
        )


def find(config: UserConfig, now: datetime.datetime, *, workspaces: bool, images: bool) -> Found:
    """Everything there is to clean up; it's only read."""
    runs = cleanup.find_runs(config)
    found = Found(stale=runs.stale, state=cleanup.find_orphaned_state(config, now))
    found.verified_store = revoke.open_store(config)
    if found.verified_store is not None:
        found.gone = cleanup.find_gone_projects(found.verified_store)
        found.leftovers = cleanup.find_store_leftovers(found.verified_store)
    # The stale runs go first, so what they used is free.
    if workspaces:
        found.workspaces = cleanup.find_workspaces(config, runs.running)
    if images:
        found.images = cleanup.find_images(config, runs.running)
    return found


def show(found: Found) -> None:
    """List what was found, by kind."""
    def section(title: str, lines: Sequence[str]) -> None:
        if lines:
            print(f"{title}:")
            for line in lines:
                print("  " + review.clean(line))

    section("Environments whose `scoter run` is gone (stopped as `scoter kill` does; their workspaces kept)", [
        f"{env.host}  {'running' if env.suc else 'reserved'}  {env.project_dir or '-'}  since {env.created_at}"
        for env in found.stale
    ])
    section("A run's files no environment uses", [
        f"{path}  ({cleanup.dir_size(path)} bytes)" for path in found.state
    ])
    gone: list[str] = []
    for env in found.gone:
        record = env.record()
        count = len(record.acceptances) if record else 0
        gone.append(f"{env.project_dir}  ({count} acceptance{'s' if count != 1 else ''})")
    section("Accepted settings of projects whose directory is gone", gone)
    section(
        f"The verified store's leftovers ({found.leftovers.size} bytes)" if found.leftovers else "",
        found.leftovers.describe(),
    )
    section("Workspaces no running environment uses (anything not fetched back is lost)", [
        f"{path}  ({cleanup.dir_size(path)} bytes)" for path in found.workspaces
    ])
    section("Images no running environment uses", found.images)


def remove(config: UserConfig, found: Found, now: datetime.datetime) -> int:
    """Remove what was found, as it still is; 0 when all of it went."""
    failed = False

    def step(what: str, call: Callable[[], None]) -> None:
        nonlocal failed
        try:
            call()
        except (OSError, SurfScoterErr) as err:
            print(f"Couldn't remove {what}: {review.clean(str(err))}", file=sys.stderr)
            failed = True

    for env in found.stale:
        def stop(env: EnvironmentDesc = env) -> None:
            nonlocal failed
            # Only if it's still stale: its run may have been started again meanwhile.
            if cleanup.is_stale(config, env.host) and main_kill.run_kill(
                config, host=env.host, fetch_branch=None, force_clean=False,
            ) != 0:
                failed = True
        step(env.host, stop)
    step("the run's files", lambda: _say_removed(cleanup.remove_orphaned_state(config, found.state, now)))
    for gone in found.gone:
        def revoke_gone(env: revoke.Environment = gone) -> None:
            revoked = cleanup.remove_gone_project(env)
            print(f"Revoked {len(revoked)} acceptance{'s' if len(revoked) != 1 else ''} of {env.project_dir}.")
        step(gone.project_dir, revoke_gone)
    if found.verified_store is not None:
        verified_store = found.verified_store

        def leftovers() -> None:
            # Again after the revoking, which can leave none.
            if cleanup.find_store_leftovers(verified_store):
                removed = cleanup.remove_store_leftovers(verified_store)
                for line in removed.describe():
                    print(f"Removed {line}.")
        step("the verified store's leftovers", leftovers)
    if found.workspaces:
        step("the workspaces", lambda: _say_removed(cleanup.remove_workspaces(config, found.workspaces)))
    if found.images:
        def images() -> None:
            for name in cleanup.remove_images(config, found.images):
                print(f"Removed {name}.")
        step("the images", images)
    return 1 if failed else 0


def _say_removed(paths: Sequence[Path]) -> None:
    for path in paths:
        print(f"Removed {path}.")


def run_cleanup(
    config: UserConfig,
    *,
    workspaces: bool = False,
    images: bool = False,
    dry_run: bool = False,
    force: bool = False,
    ask: Callable[[str], str] | None = None,
) -> int:
    """List what there is to clean up, then remove it: with `force`, or when the user says so."""
    now = datetime.datetime.now(datetime.UTC)
    try:
        found = find(config, now, workspaces=workspaces, images=images)
    except (OSError, SurfScoterErr) as err:
        print(f"Couldn't look for what to clean up: {review.clean(str(err))}", file=sys.stderr)
        return 1
    if not found:
        print("Nothing to clean up.")
        return 0
    show(found)
    if dry_run:
        print("Nothing was removed (--dry-run).")
        return 0
    if not force:
        if ask is None:
            if not sys.stdin.isatty():
                print("Nothing was removed: give --force to remove these without being asked.")
                return 0
            ask = input
        try:
            answer = ask(PROMPT).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = ""
        if answer not in ("y", "yes"):
            print("Nothing was removed.")
            return 0
    return remove(config, found, now)


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "Find what Surf Scoter left behind that nothing else removes, list it, and remove it "
        "once you say so: environments whose run is gone, a run's files no environment uses, "
        "the accepted settings of projects whose directory is gone, and the verified store's "
        "leftovers.  Expired acceptances stay: `scoter revoke` removes them."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE
    parser.add_argument(
        "--workspaces",
        action="store_true",
        help=(
            "Also remove the workspaces no running environment uses.  Anything in them not "
            "fetched back into its project is lost."
        ),
    )
    parser.add_argument(
        "--images",
        action="store_true",
        help="Also remove the images Surf Scoter built that no running environment uses.",
    )
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument(
        "--dry-run", action="store_true", dest="dry_run", help="Only list what would be removed.",
    )
    choice.add_argument(
        "--force", action="store_true", help="Remove what's found without asking.",
    )
    args = parser.parse_args(argv)
    return run_cleanup(
        config, workspaces=args.workspaces, images=args.images, dry_run=args.dry_run, force=args.force,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv))
