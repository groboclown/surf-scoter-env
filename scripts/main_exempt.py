#SPDX:MIT
"""Surf Scoter Sub-command: exempt

Starts an exemption on the project: a window in which its runs don't stop
for a review.  Each run uses your last acceptance's settings, project file
and `dev-env.yaml` files, with the container files as they are; changes to
the project file and `dev-env.yaml` files are ignored until `scoter verify
--with-exemption` picks them up.  It holds for the branch it was started
on.  See step 21 of docs/contributors/projects-v1.md.
"""
import sys
from collections.abc import Sequence

from surf_scoter_lib import BAD_EXECUTION_ARGS_CODE, UserConfig, gen_parser, review
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.verified import exempt, records


def run_exempt(config: UserConfig, length: str | None, *, end: bool) -> int:
    """Start (or move the end of) the project's exemption, or `end` it."""
    try:
        if end:
            ended = exempt.end(config)
            print("The exemption ended." if ended else "There's no exemption to end.")
            return 0
        exemption = exempt.start(
            config, records.now_utc(), exempt.parse_duration(length) if length else None,
        )
    except (OSError, SurfScoterErr) as err:
        print(review.clean(str(err)), file=sys.stderr)
        return 1
    on = f" on {review.clean(exemption.branch)}" if exemption.branch else ""
    print(
        f"Exempt{on} until {exemption.until.isoformat()}, from your acceptance {exemption.acceptance}.  "
        "Runs use its settings, with the container files as they are; run "
        "`scoter verify --with-exemption` to pick up changes to the project file or a "
        "`dev-env.yaml`, and `scoter exempt --end` to end it."
    )
    return 0


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "Start an exemption on the project: for a while, its runs don't stop for a review.  "
        "They use your last acceptance's settings, project file and dev-env.yaml files, with "
        "the container files as they are.  It holds for the current branch."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE
    parser.add_argument(
        "--for",
        dest="length",
        default=None,
        metavar="LENGTH",
        help=(
            "How long: such as 8h, 1d or 2d12h.  By default, `verification: values: exempt-days`; "
            "at most `max-exempt-days`, and never past the acceptance's expiry."
        ),
    )
    parser.add_argument("--end", action="store_true", help="End the project's exemption now.")
    args = parser.parse_args(argv)
    if args.end and args.length:
        parser.error("give --for or --end, not both")
    return run_exempt(config, args.length, end=args.end)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
