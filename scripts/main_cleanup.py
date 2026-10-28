#SPDX:MIT
"""Surf Scoter Sub-command: cleanup

Finds what Surf Scoter left behind that nothing else removes, lists it,
and removes it once you say so: environments whose `scoter run` is gone
(stopped as `scoter kill` does), a run's state no record uses, the
accepted settings of projects whose directory is gone, and the verified
store's leftovers.  With `--project-state`, the project state directories
(each project's clone, settings and resources) no running environment uses;
with `--persistent-stores`, the persistent store volumes none uses; with
`--images`, the images it built that none uses.
The ssh config entries of environments that aren't running go as `ssh:
values: config-cleanup` says: with `ask`, they're asked about on their own,
even with `--force`.  See docs/cleaning-up.md.
"""
import datetime
import enum
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import main_kill
from surf_scoter_lib import (
    BAD_EXECUTION_ARGS_CODE,
    UserConfig,
    argutil,
    cleanup,
    gen_parser,
    review,
    ssh_hosts,
)
from surf_scoter_lib.active_env import EnvironmentDesc
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.verified import revoke, store

PROMPT = "Remove these? [y/N]: "
SSH_PROMPT = "Remove these ssh config entries? [y/N]: "


class Extras(enum.Flag):
    """What else the cleanup looks for, besides what nothing else removes."""

    NONE = 0
    # The project state directories no running environment uses (`--project-state`).
    PROJECT_STATE = enum.auto()
    # The persistent store volumes no running environment uses (`--persistent-stores`).
    PERSISTENT_STORES = enum.auto()
    # The images no running environment uses (`--images`).
    IMAGES = enum.auto()


class Confirm(enum.Enum):
    """Whether the cleanup removes what it finds."""

    # Remove it when the user says so.
    ASK = "ask"
    # Only list it (`--dry-run`).
    DRY_RUN = "dry-run"
    # Remove it without asking (`--force`); the ssh entries are still asked about.
    FORCE = "force"


@dataclass
class Found:
    """What the cleanup found."""

    stale: list[EnvironmentDesc] = field(default_factory=list)
    state: list[Path] = field(default_factory=list)
    gone: list[revoke.Environment] = field(default_factory=list)
    leftovers: cleanup.StoreLeftovers = field(default_factory=cleanup.StoreLeftovers)
    project_state: list[Path] = field(default_factory=list)
    persistent_stores: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    verified_store: store.VerifiedStore | None = None
    # The ssh config's entries for environments that aren't running, and
    # `ssh: values: config-cleanup`; with `no`, only how many there are.
    ssh: list[str] = field(default_factory=list)
    ssh_mode: str = ssh_hosts.CLEANUP_ASK
    ssh_kept: int = 0
    ssh_problem: str | None = None

    def others(self) -> bool:
        """Whether there's anything but ssh entries."""
        return bool(
            self.stale or self.state or self.gone or self.leftovers or self.project_state
            or self.persistent_stores or self.images
        )

    def __bool__(self) -> bool:
        return self.others() or bool(self.ssh)


def find(config: UserConfig, now: datetime.datetime, *, extras: Extras) -> Found:
    """Everything there is to clean up; it's only read."""
    runs = cleanup.find_runs(config)
    found = Found(stale=runs.stale, state=cleanup.find_orphaned_state(config, now))
    found.verified_store = revoke.open_store(config)
    if found.verified_store is not None:
        found.gone = cleanup.find_gone_projects(found.verified_store)
        found.leftovers = cleanup.find_store_leftovers(found.verified_store)
    # The stale runs go first, so what they used is free.
    if Extras.PROJECT_STATE in extras:
        found.project_state = cleanup.find_project_state(config, runs.running)
    if Extras.PERSISTENT_STORES in extras:
        found.persistent_stores = cleanup.find_persistent_stores(config, runs.running)
    if Extras.IMAGES in extras:
        found.images = cleanup.find_images(config, runs.running)
    found.ssh_mode = ssh_hosts.cleanup_mode(config)
    try:
        hosts = ssh_hosts.leftover_hosts(config, [env.host for env in runs.running])
    except SurfScoterErr as err:
        found.ssh_problem = str(err)
        hosts = []
    if found.ssh_mode == ssh_hosts.CLEANUP_NO:
        found.ssh_kept = len(hosts)
    else:
        found.ssh = hosts
    return found


def show(found: Found) -> None:
    """List what was found, by kind."""
    def section(title: str, lines: Sequence[str]) -> None:
        if lines:
            print(f"{title}:")
            for line in lines:
                print("  " + review.clean(line))

    section("Environments whose `scoter run` is gone (stopped as `scoter kill` does; their project state kept)", [
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
    section("Project state no running environment uses (anything in its clone not fetched back is lost)", [
        f"{path}  ({cleanup.dir_size(path)} bytes)" for path in found.project_state
    ])
    section("Persistent stores no running environment uses", found.persistent_stores)
    section("Images no running environment uses", found.images)
    section(
        "SSH config entries for environments that aren't running"
        + (" (asked about on their own)" if found.ssh_mode == ssh_hosts.CLEANUP_ASK and found.others() else ""),
        found.ssh,
    )
    if found.ssh_kept:
        print(
            f"Your ssh config has {found.ssh_kept} entr{'ies' if found.ssh_kept != 1 else 'y'} for "
            "environments that aren't running, kept as `ssh: values: config-cleanup: no` says."
        )
    if found.ssh_problem:
        print(f"Your ssh config wasn't looked at: {review.clean(found.ssh_problem)}")


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
            # Its ssh entries are asked about with the others, below.
            if cleanup.is_stale(config, env.host) and main_kill.run_kill(
                config, host=env.host, fetch_branch=None,
                ssh=main_kill.SshEntries.KEEP,
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
    if found.project_state:
        step("the project state", lambda: _say_removed(cleanup.remove_project_state(config, found.project_state)))
    if found.persistent_stores:
        def stores() -> None:
            for name in cleanup.remove_persistent_stores(config, found.persistent_stores):
                print(f"Removed {name}.")
        step("the persistent stores", stores)
    if found.images:
        def images() -> None:
            for name in cleanup.remove_images(config, found.images):
                print(f"Removed {name}.")
        step("the images", images)
    if found.ssh and found.ssh_mode == ssh_hosts.CLEANUP_AUTO:
        step("the ssh config entries", lambda: _say_ssh_removed(config, found.ssh))
    return 1 if failed else 0


def _say_ssh_removed(config: UserConfig, hosts: Sequence[str]) -> None:
    for host in ssh_hosts.remove_hosts(config, hosts):
        print(f"Removed the ssh entries for {host}.")


def _ask(ask: Callable[[str], str] | None, prompt: str, without: str) -> bool:
    """Whether the user says yes; without a terminal to ask at (and no `ask`), no, saying `without`."""
    if ask is None:
        if not argutil.can_ask():
            print(without)
            return False
        ask = input
    try:
        answer = ask(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        answer = ""
    return answer in ("y", "yes")


def _say_removed(paths: Sequence[Path]) -> None:
    for path in paths:
        print(f"Removed {path}.")


def run_cleanup(
    config: UserConfig,
    *,
    extras: Extras = Extras.NONE,
    confirm: Confirm = Confirm.ASK,
    ask: Callable[[str], str] | None = None,
) -> int:
    """List what there is to clean up, then remove it: with `Confirm.FORCE`, or when the user says so."""
    now = datetime.datetime.now(datetime.UTC)
    try:
        found = find(config, now, extras=extras)
    except (OSError, SurfScoterErr) as err:
        print(f"Couldn't look for what to clean up: {review.clean(str(err))}", file=sys.stderr)
        return 1
    if not found:
        print("Nothing to clean up.")
        # What it didn't look at, or keeps.
        show(found)
        return 0
    show(found)
    if confirm == Confirm.DRY_RUN:
        print("Nothing was removed (--dry-run).")
        return 0
    code = 0
    if found.others() or (found.ssh and found.ssh_mode == ssh_hosts.CLEANUP_AUTO):
        if confirm == Confirm.FORCE or _ask(ask, PROMPT, "Nothing was removed: give --force to remove these without being asked."):
            code = remove(config, found, now)
        else:
            print("Nothing was removed.")
            return 0
    if found.ssh and found.ssh_mode == ssh_hosts.CLEANUP_ASK:
        # `ask` is the user's say over their ssh config: `--force` doesn't answer for them.
        if _ask(ask, SSH_PROMPT, "The ssh config entries were kept: there's no terminal to ask at."):
            try:
                _say_ssh_removed(config, found.ssh)
            except (OSError, SurfScoterErr) as err:
                print(f"Couldn't remove the ssh config entries: {review.clean(str(err))}", file=sys.stderr)
                code = 1
        else:
            print("The ssh config entries were kept.")
    return code


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
        "--project-state",
        action="store_true",
        dest="project_state",
        help=(
            "Also remove the project state directories (each project's clone, settings and "
            "resources) no running environment uses.  Anything in a clone not fetched back "
            "into its project is lost."
        ),
    )
    parser.add_argument(
        "--persistent-stores",
        action="store_true",
        dest="persistent_stores",
        help="Also remove the dev containers' persistent stores no running environment uses.",
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
    extras = Extras.NONE
    if args.project_state:
        extras |= Extras.PROJECT_STATE
    if args.persistent_stores:
        extras |= Extras.PERSISTENT_STORES
    if args.images:
        extras |= Extras.IMAGES
    confirm = Confirm.ASK
    if args.dry_run:
        confirm = Confirm.DRY_RUN
    elif args.force:
        confirm = Confirm.FORCE
    return run_cleanup(config, extras=extras, confirm=confirm)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
