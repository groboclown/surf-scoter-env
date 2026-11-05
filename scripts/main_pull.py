#SPDX:MIT
"""Pull in this project's Surf Scoter configuration into your personal settings.

A Surf Scoter sub-command.

    scoter pull [--ref REF | --working-tree] [--name NAME] [--allow-opaque PATH ...]
                [--keep-working-dir]

seeds a working directory from the project, runs the setup's reviewer
(`pull: runs: reviewer`), and approves when the reviewer says the developer
did.  The plumbing: each phase is its own sub-sub-command, with a fixed
command line no setting can change.

    scoter pull seed [--ref REF | --working-tree]
    scoter pull analyze WORKDIR-ID [--output FILE]
    scoter pull changed WORKDIR-ID ANALYSIS-ID
    scoter pull approve WORKDIR-ID ANALYSIS-ID [--name NAME]

Each prints JSON.  `analyze`, `changed` and `approve` take only ids that
`seed` registered, read through a SUC that matches the one at seeding
(VER-29), never a path.  `scoter` runs this with the project found but not
parsed (`subcmd-project-found.txt`): it reads the project's files only from
copies.
"""
import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from surf_scoter_lib import (
    BAD_EXECUTION_ARGS_CODE,
    UserConfig,
    fileutil,
    gen_parser,
    generator,
    review,
    run_config,
)
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.pull import analyze, approve, lifecycle, porcelain, seed


def _print(data: dict) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True))


def run_pull(config: UserConfig, args: argparse.Namespace) -> int:
    """Run the phase `args.phase`."""
    phase = args.phase
    try:
        if phase is None:
            link = lifecycle.context(config).link()
            if link is not None and link.unlocked is not None:
                print(
                    "Your configuration is unlocked: `scoter lock` (or `scoter lock --revert`) first, "
                    "so a pull never drops your edits.",
                    file=sys.stderr,
                )
                return 1
            return porcelain.pull(config, porcelain.Options(
                ref=args.pull_ref, working_tree=args.pull_working_tree,
                keep_working_dir=args.keep_working_dir, name=args.pull_name,
                allow_opaque=args.allow_opaque,
            ))
        if phase == "seed":
            _print(seed.seed(
                config, generate=generator.run, source=args.source, ref=args.ref,
                working_tree=args.working_tree,
            ).to_json())
        elif phase == "analyze":
            report = analyze.analyze(config, args.workdir, generate=generator.run)
            output = args.output
            if output:
                path = Path(output).absolute()
                fileutil.write_private(path.parent, [path.name], analyze.to_text(report).encode("ascii"))
                _print({"id": report["id"], "output": str(path)})
            else:
                sys.stdout.write(analyze.to_text(report))
        elif phase == "changed":
            _print(analyze.changed(config, args.workdir, args.analysis))
        else:
            _print(approve.approve(
                config, args.workdir, args.analysis, generate=generator.run, name=args.name,
                running=run_config.running_config_ids(config),
            ).to_json())
    except (OSError, SurfScoterErr) as err:
        print(review.clean(str(err)), file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "The pull's phases: seed a working directory from the project, analyze it, list what "
        "changed since an analysis, and approve it as a locked configuration."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--ref", dest="pull_ref", default=None,
        help="Pull the files as committed at this ref (default: HEAD).",
    )
    source.add_argument(
        "--working-tree", dest="pull_working_tree", action="store_true",
        help="Pull the files on disk, committed or not; the review flags those that aren't committed.",
    )
    parser.add_argument(
        "--name", dest="pull_name", default=None,
        help="The configuration's name (default: default).  The one it replaces is kept as NAME.previous.",
    )
    parser.add_argument(
        "--allow-opaque", action="append", default=[], metavar="PATH",
        help="Allow this file, which the review can't show (too large, or not text), to be approved unseen.",
    )
    parser.add_argument(
        "--keep-working-dir", action="store_true",
        help="Keep the working directory afterwards, for debugging; it says where.",
    )
    phases = parser.add_subparsers(dest="phase", required=False)
    seeding = phases.add_parser("seed", help="Copy the project's files into a new working directory.")
    seeding.add_argument(
        "--from", dest="source", choices=seed.SOURCES, default=seed.FROM_PROJECT,
        help="Seed from the project (the default), your unlocked copy, or the configuration in use.",
    )
    seed_source = seeding.add_mutually_exclusive_group()
    seed_source.add_argument(
        "--ref", default=seed.DEFAULT_REF,
        help="Pull the files as committed at this ref (default: HEAD).",
    )
    seed_source.add_argument(
        "--working-tree", action="store_true",
        help="Pull the files on disk, committed or not, rather than from a commit.",
    )
    analyzing = phases.add_parser("analyze", help="Check a working directory, and print the report.")
    analyzing.add_argument("workdir", metavar="WORKDIR-ID")
    analyzing.add_argument("--output", metavar="FILE", help="Write the report here instead.")
    changing = phases.add_parser("changed", help="List the files changed since an analysis.")
    changing.add_argument("workdir", metavar="WORKDIR-ID")
    changing.add_argument("analysis", metavar="ANALYSIS-ID")
    approving = phases.add_parser("approve", help="Store the working directory as a locked configuration.")
    approving.add_argument("workdir", metavar="WORKDIR-ID")
    approving.add_argument("analysis", metavar="ANALYSIS-ID")
    approving.add_argument(
        "--name", default=None,
        help=(
            "The configuration's name (default: the name of the one it replaces, or default).  From "
            "the project, the one it replaces is kept as NAME.previous."
        ),
    )
    args = parser.parse_args(argv)
    if args.phase == "seed" and args.working_tree:
        args.ref = seed.DEFAULT_REF
    return run_pull(config, args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
