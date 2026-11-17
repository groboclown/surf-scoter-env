#SPDX:MIT
"""`scoter pull`: seed, review and approve, as one command.

The porcelain runs each phase as `scoter pull PHASE`, handing on its own SUC
(`subcommand.run`), and the setup's reviewer (`pull: runs: reviewer`)
between them.  So a replaced reviewer only decides *whether* to approve: it
can't skip seeding, and `approve` checks everything again.  The working
directory is removed however the pull ends, unless `--keep-working-dir`
keeps it (VER-34).
"""
import json
import os
import subprocess  # nosec: B404  # Runs the setup's reviewer, as a list.
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .. import jdata, review, subcommand
from ..user_config import UserConfig
from . import reviewer, seed, workdir

REVIEWER_SECTION = "pull"
REVIEWER_RUN = "reviewer"


@dataclass
class Options:
    """What `scoter pull` was asked to do."""

    ref: str | None = None
    working_tree: bool = False
    keep_working_dir: bool = False
    name: str | None = None
    allow_opaque: list[str] = field(default_factory=list)
    # Where to seed from: the project (`scoter pull`), the unlocked copy
    # (`scoter lock`) or the configuration in use (`scoter review`).
    source: str = seed.FROM_PROJECT
    # The finding ids an earlier review showed (`scoter review`, VER-41).
    known_findings: list[str] = field(default_factory=list)


def _phase(config: UserConfig, args: Sequence[str]) -> jdata.DictJsonData:
    finished = subcommand.run(config, "pull", args, capture=True)
    if finished.code != 0 or finished.stdout is None:
        raise workdir.PullErr(f"`scoter pull {args[0]}` failed")
    value = json.loads(finished.stdout)
    if not isinstance(value, dict):
        raise workdir.PullErr(f"`scoter pull {args[0]}` printed something unexpected")
    return value


def run_reviewer(config: UserConfig, workdir_id: str, session: Path) -> int:
    """Run the setup's reviewer, with the porcelain's SUC; its exit code."""
    with subcommand.suc_file(config) as universal:
        cmd = config.require_run(
            REVIEWER_SECTION, REVIEWER_RUN,
            {"universal": universal, "workdir": workdir_id, "session": str(session)},
            whole_args=True,
        )
        if not cmd or not os.path.isabs(cmd[0]):
            raise workdir.PullErr(
                "The setup's `pull: runs: reviewer` must start with a program, found as an absolute path."
            )
        return subprocess.run(  # nosec: B603  # The setup's reviewer, as a list.
            cmd, check=False, shell=False, env=config.command_env(REVIEWER_SECTION),
        ).returncode


def pull(
    config: UserConfig,
    options: Options,
    *,
    say: Callable[[str], None] = print,
    review_with: Callable[[UserConfig, str, Path], int] = run_reviewer,
) -> int:
    """Seed, review and approve: 0 when a configuration was approved, or there was nothing to pull."""
    seed_args = ["seed", "--from", options.source]
    if options.working_tree:
        seed_args.append("--working-tree")
    elif options.ref:
        seed_args.extend(("--ref", options.ref))
    seeded = _phase(config, seed_args)
    workdir_id = jdata.req_str(seeded, "id")
    try:
        for warning in jdata.find_str_list(seeded, "warnings"):
            say(f"Warning: {review.clean(warning)}")
        if seeded.get("unchanged") is True:
            say("Nothing to pull: the project's files, and your setup's checks, are as at your last pull.")
            return 0
        with tempfile.TemporaryDirectory(prefix="surf-scoter-") as tmp:
            session = Path(tmp) / "session.json"
            reviewer.write_session(session, reviewer.Session(
                allow_opaque=list(options.allow_opaque), known_findings=list(options.known_findings),
            ))
            review_with(config, workdir_id, session)
            decided = reviewer.read_session(session)
        if decided.result != reviewer.APPROVE or decided.analysis is None:
            say("Not approved: nothing changed.")
            return 1
        approve_args = ["approve", workdir_id, decided.analysis]
        if options.name:
            approve_args.extend(("--name", options.name))
        approved = _phase(config, approve_args)
        name = review.clean(jdata.req_str(approved, "name"))
        say(f"Approved: the configuration `{name}` ({jdata.req_str(approved, 'configuration')}) is now in use.")
        previous = jdata.find_str(approved, "previous")
        if previous:
            say(f"The one it replaced is kept as `{review.clean(previous)}`; `scoter switch` goes back.")
        for change in jdata.find_str_list(approved, "host-changes"):
            say(f"  Host commands: {review.clean(change)}")
        return 0
    finally:
        if options.keep_working_dir:
            path = workdir.pull_root(config) / workdir.WORK_DIR / workdir_id
            say(
                f"Kept the working directory at {path}.  It holds the project's files, unreviewed "
                "unless you approved them; `scoter cleanup` removes it."
            )
        else:
            workdir.remove(config, workdir_id)
