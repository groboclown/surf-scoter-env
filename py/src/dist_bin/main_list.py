#SPDX:MIT
"""List your project configurations.

A Surf Scoter sub-command.

Lists every project you have configurations for, with each configuration:
its id, name, when it was made, and whether it's in use.  Give an id to
`scoter remove` to remove it.  With `-q`, only the ids, one per line, so
`scoter remove $(scoter list -q)` removes them all.
"""
import enum
import os
import sys
from collections.abc import Sequence

from surf_scoter_lib import BAD_EXECUTION_ARGS_CODE, UserConfig, gen_parser, review
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.verified import remove


class Listing(enum.Enum):
    """How much `run_list` shows."""

    # The projects, their configurations, and a summary.
    FULL = "full"
    # Only the configurations' ids, one per line (`--quiet`).
    IDS = "ids"


def run_list(config: UserConfig, *, listing: Listing = Listing.FULL) -> int:
    """List the projects and their configurations; with `Listing.IDS`, only the configurations' ids."""
    quiet = listing == Listing.IDS
    try:
        verified_store = remove.open_store(config)
        listed = [] if verified_store is None else [
            (env, env.link(), env.configs()) for env in remove.environments(verified_store)
        ]
        size = 0 if verified_store is None else verified_store.blob_size()
    except (OSError, SurfScoterErr) as err:
        print(f"Couldn't read your configurations: {review.clean(str(err))}", file=sys.stderr)
        return 1
    count = 0
    for env, link, found in listed:
        count += len(found)
        if quiet:
            for each in found:
                print(each.id)
            continue
        gone = "" if os.path.isdir(env.project_dir) else "  (the directory is gone)"
        print(f"{review.clean(env.project_dir)}{gone}")
        if link is not None and link.unlocked is not None:
            print(f"  unlocked since {link.unlocked.at.isoformat()}")
        if link is not None and link.last_reviewed is not None:
            print(f"  last reviewed {link.last_reviewed.isoformat()}")
        for each in found:
            print("  " + review.clean(remove.describe(each, link)))
    if not quiet:
        if not count:
            print("You have no configurations.  `scoter pull` makes one from a project's files.")
        else:
            projects = sum(1 for _, _, found in listed if found)
            print(
                f"{count} configuration{'s' if count != 1 else ''} of {projects} "
                f"project{'s' if projects != 1 else ''}; their files take {size} bytes.  "
                "Remove one with `scoter remove ID`."
            )
    return 0


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "List every project you have configurations for, with each one's id, name, age and "
        "whether it's in use.  Give an id to `scoter remove` to remove it; "
        "`scoter remove $(scoter list -q)` removes them all."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        dest="quiet",
        help="Show only the configurations' ids, one per line, for `scoter remove`.",
    )
    args = parser.parse_args(argv)
    return run_list(config, listing=Listing.IDS if args.quiet else Listing.FULL)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
