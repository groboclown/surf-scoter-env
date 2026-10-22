#SPDX:MIT
"""Surf Scoter Sub-command: list-accepted

Lists every project whose settings you've accepted, with each acceptance:
its id, kind, branch, fingerprint, expiry and any exemption.  Give an id to
`scoter revoke` to revoke it.  With `-q`, only the ids, one per line, so
`scoter revoke $(scoter list-accepted -q)` revokes them all.
"""
import datetime
import enum
import os
import sys
from collections.abc import Sequence

from surf_scoter_lib import BAD_EXECUTION_ARGS_CODE, UserConfig, gen_parser, review
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.verified import revoke


class Listing(enum.Enum):
    """How much `run_list` shows."""

    # The projects, their acceptances, and a summary.
    FULL = "full"
    # Only the acceptances' ids, one per line (`--quiet`).
    IDS = "ids"


def run_list(config: UserConfig, *, listing: Listing = Listing.FULL) -> int:
    """List the accepted projects and their acceptances; with `Listing.IDS`, only the acceptances' ids."""
    quiet = listing == Listing.IDS
    now = datetime.datetime.now(datetime.UTC)
    try:
        verified_store = revoke.open_store(config)
        listed = [] if verified_store is None else [
            (env, env.record()) for env in revoke.environments(verified_store)
        ]
        size = 0 if verified_store is None else verified_store.blob_size()
    except (OSError, SurfScoterErr) as err:
        print(f"Couldn't read your accepted settings: {review.clean(str(err))}", file=sys.stderr)
        return 1
    count = 0
    for env, record in listed:
        acceptances = record.acceptances if record is not None else []
        count += len(acceptances)
        if quiet:
            for acceptance in acceptances:
                print(acceptance.id)
            continue
        gone = "" if os.path.isdir(env.project_dir) else "  (the directory is gone)"
        print(f"{review.clean(env.project_dir)}{gone}")
        for acceptance in acceptances:
            print("  " + review.clean(revoke.describe(acceptance, now)))
        if record is not None and record.exemption is not None:
            print("  " + review.clean(revoke.describe_exemption(record.exemption, now)))
    if not quiet:
        if not count:
            print("You haven't accepted any project's settings.")
        else:
            projects = sum(1 for _, record in listed if record is not None and record.acceptances)
            print(
                f"{count} acceptance{'s' if count != 1 else ''} of {projects} "
                f"project{'s' if projects != 1 else ''}; their copies of the projects' files "
                f"take {size} bytes.  Revoke one with `scoter revoke ID`."
            )
    return 0


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "List every project whose settings you've accepted, with each acceptance's id, "
        "kind, branch, fingerprint, expiry and exemption.  Give an id to `scoter revoke` "
        "to revoke it; `scoter revoke $(scoter list-accepted -q)` revokes them all."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        dest="quiet",
        help="Show only the acceptances' ids, one per line, for `scoter revoke`.",
    )
    args = parser.parse_args(argv)
    return run_list(config, listing=Listing.IDS if args.quiet else Listing.FULL)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
