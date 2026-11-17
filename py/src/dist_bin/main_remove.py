#SPDX:MIT
"""Remove locked configurations.

A Surf Scoter sub-command.

Removes your locked configurations: by their id (as `scoter list`
shows it), for any project; or, for the current project, by name, or all of
them.  Only your copies go: the project's own files are never touched.  Removing the one in use leaves the project with none: its next run
asks for `scoter pull`.  One a running environment uses goes once it stops.
Without any, lists the current project's configurations.

`scoter remove $(scoter list -q)` removes every configuration.
"""
import sys
from collections.abc import Sequence

from surf_scoter_lib import (
    BAD_EXECUTION_ARGS_CODE,
    UserConfig,
    gen_parser,
    review,
    run_config,
)
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.verified import remove

NO_PROJECT = (
    "There's no project here.  Run this in a project, or give the ids of the configurations "
    "to remove, as `scoter list` shows them."
)


def run_remove(
    config: UserConfig, names: Sequence[str], *, pick: remove.Pick = remove.Pick.NAMED,
) -> int:
    """Remove the configurations `names` choose (or, with `Pick.ALL`, all the project's); without any, list them."""
    everything = pick == remove.Pick.ALL
    try:
        verified_store = remove.open_store(config)
        if verified_store is None:
            print("You have no configurations: there's nothing to remove.")
            return 1 if names else 0
        chosen = {env: [c.id for c in found] for env, found in remove.find_ids(verified_store, names).items()}
        by_id = {i for ids in chosen.values() for i in ids}
        rest = [name for name in names if name not in by_id]
        env = remove.environment(config, verified_store)
        running = run_config.running_config_ids(config)
    except (OSError, SurfScoterErr) as err:
        print(f"Couldn't read your configurations: {review.clean(str(err))}", file=sys.stderr)
        return 1

    if rest or everything or not names:
        # Names, `--all` and the listing are the current project's.
        if env is None:
            print(NO_PROJECT, file=sys.stderr)
            return 1
        link = env.link()
        if not names and not everything:
            listed = env.configs()
            if not listed:
                print("This project has no configurations: there's nothing to remove.")
                return 0
            print(f"The configurations of {review.clean(env.project_dir)}, newest first:")
            for found in listed:
                print("  " + review.clean(remove.describe(found, link)))
            print("Remove them by name or id (`scoter remove NAME...`), or all (`--all`).")
            return 0
        selected, unmatched = remove.select(env, rest)
        if unmatched:
            for name in unmatched:
                print(f"No configuration of this project is named, or has the id, `{review.clean(name)}`.", file=sys.stderr)
            print("Nothing was removed.", file=sys.stderr)
            return 1
        chosen.setdefault(env, [])
        chosen[env] = [*chosen[env], *(c.id for c in selected if c.id not in chosen[env])]

    removed: list[tuple[str, str]] = []
    try:
        for found_env, ids in chosen.items():
            done = remove.remove(found_env, ids, running, everything=everything and found_env == env)
            removed.extend((found_env.project_dir, i) for i in done)
    except (OSError, SurfScoterErr) as err:
        print(f"Couldn't remove them: {review.clean(str(err))}", file=sys.stderr)
        if removed:
            print(f"Removed {len(removed)} before that.", file=sys.stderr)
        return 1
    print(f"Removed {len(removed)}:")
    for project_dir, config_id in removed:
        print(f"  {review.clean(project_dir)}: {config_id}")
    print("A project left without a configuration in use asks for `scoter pull` at its next run.")
    return 0


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "Remove your locked configurations: by id (as `scoter list` shows it), for "
        "any project; or, for this project, by name, or all of them.  Only your copies go: the "
        "project's own files are never touched.  Without any, lists this "
        "project's configurations.  `scoter remove $(scoter list -q)` removes them all."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE
    parser.add_argument(
        "names", nargs="*", metavar="NAME-OR-ID",
        help="A configuration's id, or this project's configuration's name, to remove.",
    )
    parser.add_argument(
        "--all", action="store_true", dest="everything",
        help="Remove all of this project's configurations, and forget the project.",
    )
    args = parser.parse_args(argv)
    if args.everything and args.names:
        parser.error("give names and ids, or --all, not both")
    return run_remove(config, args.names, pick=remove.Pick.ALL if args.everything else remove.Pick.NAMED)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
