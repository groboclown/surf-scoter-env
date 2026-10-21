#SPDX:MIT
"""Surf Scoter Sub-command: verify

Writes the review file for the project: its suggested settings, resolved,
with the checks' findings as comments, for you to accept, edit or reject.
See step 12 of docs/contributors/projects-v1.md for the file.
"""
import datetime
import os
import subprocess  # nosec: B404  # Runs the setup's own commands.
import sys
from collections.abc import Sequence
from pathlib import Path

from surf_scoter_lib import (
    BAD_EXECUTION_ARGS_CODE,
    UserConfig,
    checks,
    gen_parser,
    review,
)
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.verified import lookup


def run_verify(config: UserConfig, output: Path | None) -> int:
    """Write the review file to `output`, or to stdout."""
    if config.project_setup() is None:
        print("No project here: run this in a project, or give --project-dir.", file=sys.stderr)
        return 1
    report = checks.check_project(config)
    context = review.Context(
        report=report,
        now=datetime.datetime.now(datetime.UTC),
        files=review.project_snapshot(config),
        branch=current_branch(config),
        accepted_note=accepted_note(config),
    )
    text = review.build(config, context)
    if output is None:
        sys.stdout.write(text)
    else:
        write_private(output, text)
        print(f"Wrote the review to {review.clean(str(output))}", file=sys.stderr)
    refused = len(report.refused)
    warned = sum(1 for f in report.findings if f.severity == checks.Severity.WARN)
    print(f"{refused} refused, {warned} warnings.", file=sys.stderr)
    return 0


def current_branch(config: UserConfig) -> str | None:
    """The project's branch, from its VCS section's `runs: current-branch`, if it has one."""
    try:
        section = config.get_vcs_section()
    except SurfScoterErr:
        return None
    if section is None or config.raw("resolved", section, "runs", "current-branch") is None:
        return None
    root = config.active_location("vcs-root") or config.active_location("project-dir")
    try:
        args = config.require_run(section, "current-branch", {"root": str(root)})
        res = subprocess.run(  # nosec: B603  # The command comes from the user's setup.
            args, capture_output=True, check=False, shell=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError, SurfScoterErr):
        return None
    branch = res.stdout.decode("utf-8", errors="replace").strip()
    return branch if res.returncode == 0 and branch else None


def accepted_note(config: UserConfig) -> str | None:
    """A line saying whether an acceptance already matches the project's files."""
    try:
        found = lookup.find(config)
    except (OSError, ValueError, SurfScoterErr) as err:
        return f"your accepted settings couldn't be read: {err}"
    if found.match is None:
        return None
    return f"already accepted ({found.match} match, {found.acceptance}); this review would replace it"


def write_private(path: Path, text: str) -> None:
    """Write `text` to `path`, readable only by you, not following a symlink."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as out:
        out.write(text)


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "Write the project's review file: its suggested settings, resolved, with what "
        "the checks found, for you to accept, edit or reject."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE
    parser.add_argument(
        "-o",
        "--output",
        action="store",
        dest="output",
        default=None,
        help="Where to write the review file; by default, standard output.",
    )
    args = parser.parse_args(argv)
    return run_verify(config, Path(args.output) if args.output else None)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
