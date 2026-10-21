#SPDX:MIT
"""Surf Scoter Sub-command: revoke

Revokes your acceptances of projects' settings: by their id (as `scoter
list-accepted` shows it), for any project; or, for the current project, by
the branch they were accepted on, by the fingerprint of the files they were
accepted for, or all of them.  The next run asks for a review again
(`scoter verify`).  Without any, lists the current project's acceptances.

`scoter revoke $(scoter list-accepted -q)` revokes every acceptance.
"""
import datetime
import sys
from collections.abc import Sequence
from typing import TextIO

from surf_scoter_lib import BAD_EXECUTION_ARGS_CODE, UserConfig, gen_parser, review
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.verified import records, revoke

NO_PROJECT = (
    "There's no project here.  Run this in a project, or give the ids of the acceptances "
    "to revoke, as `scoter list-accepted` shows them."
)


def run_revoke(config: UserConfig, names: Sequence[str], *, everything: bool) -> int:
    """Revoke the acceptances `names` choose (or all the project's); without any, list them."""
    try:
        verified_store = revoke.open_store(config)
        if verified_store is None:
            print("You haven't accepted any project's settings: there's nothing to revoke.")
            return 1 if names else 0
        chosen = revoke.find_ids(verified_store, names)
        by_id = {a.id for found in chosen.values() for a in found}
        rest = [name for name in names if name not in by_id]
        env = revoke.environment(config, verified_store)
        record = env.record() if env is not None else None
    except (OSError, SurfScoterErr) as err:
        print(f"Couldn't read your accepted settings: {review.clean(str(err))}", file=sys.stderr)
        return 1

    if rest or everything or not names:
        # Branches, fingerprints, `--all` and the listing are the current project's.
        if env is None:
            if rest and all(revoke.ACCEPTANCE_ID.fullmatch(name) for name in rest):
                for name in rest:
                    print(f"No acceptance has the id `{name}`.", file=sys.stderr)
                print("Nothing was revoked.", file=sys.stderr)
            else:
                print(NO_PROJECT, file=sys.stderr)
            return 1
        acceptances = record.acceptances if record is not None else []
        if not names and not everything:
            if not acceptances:
                print("You haven't accepted this project's settings: there's nothing to revoke.")
                return 0
            print(f"Your acceptances of {review.clean(env.project_dir)}'s settings, newest first:")
            show(acceptances)
            print(
                "Revoke them by id, branch or fingerprint (`scoter revoke NAME...`), "
                "or all (`--all`)."
            )
            return 0
        selection = (
            revoke.select(record, rest, everything=everything)
            if record is not None else revoke.Selection(unmatched=list(rest))
        )
        if selection.unmatched:
            for name in selection.unmatched:
                print(
                    f"No acceptance has the id, or is for the branch or fingerprint, "
                    f"`{review.clean(name)}`.",
                    file=sys.stderr,
                )
            print("Nothing was revoked.  The project's acceptances:", file=sys.stderr)
            show(acceptances, file=sys.stderr)
            return 1
        if selection.chosen:
            assert env is not None  # nosec: B101  # narrowing for the type checker
            chosen.setdefault(env, [])
            chosen[env] = [*chosen[env], *(a for a in selection.chosen if a not in chosen[env])]

    removed: list[tuple[str, records.Acceptance]] = []
    try:
        for found_env, acceptances in chosen.items():
            removed.extend(
                (found_env.project_dir, a)
                for a in revoke.revoke(found_env, [a.id for a in acceptances])
            )
    except (OSError, SurfScoterErr) as err:
        print(f"Couldn't revoke them: {review.clean(str(err))}", file=sys.stderr)
        if removed:
            print(f"Revoked {len(removed)} before that.", file=sys.stderr)
        return 1
    print(f"Revoked {len(removed)}:")
    for project_dir, acceptance in removed:
        print(f"  {review.clean(project_dir)}")
        show([acceptance], indent="    ")
    print("The next run of each asks you to review its settings again (`scoter verify`).")
    return 0


def show(
    acceptances: Sequence[records.Acceptance], file: TextIO | None = None, indent: str = "  ",
) -> None:
    """One line for each acceptance."""
    now = datetime.datetime.now(datetime.UTC)
    for acceptance in acceptances:
        print(indent + review.clean(revoke.describe(acceptance, now)), file=file or sys.stdout)


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "Revoke your acceptances of projects' settings: by id (as `scoter list-accepted` "
        "shows it), for any project; or, for this project, by the branch they were accepted "
        "on, by the fingerprint of the files they were accepted for (as the review shows it, "
        "or its first 12 characters or more), or all of them.  The next run asks for a review "
        "again.  Without any, lists this project's acceptances.  "
        "`scoter revoke $(scoter list-accepted -q)` revokes every acceptance."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE
    parser.add_argument(
        "names",
        nargs="*",
        metavar="ID-BRANCH-OR-FINGERPRINT",
        help="An acceptance id, or this project's branch or fingerprint, to revoke.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="everything",
        help="Revoke all of this project's acceptances.",
    )
    args = parser.parse_args(argv)
    if args.everything and args.names:
        parser.error("give ids, branches and fingerprints, or --all, not both")
    return run_revoke(config, args.names, everything=args.everything)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
