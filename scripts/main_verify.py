#SPDX:MIT
"""Surf Scoter Sub-command: verify

Writes the review file for the project: its suggested settings, resolved,
with the checks' findings as comments, and asks you to use it, edit it or
deny it.  Using it stores your acceptance, which later runs use.
"""
import datetime
import enum
import os
import shlex
import shutil
import stat
import subprocess  # nosec: B404  # Runs the setup's own commands, the generator and your editor.
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from surf_scoter_lib import (
    BAD_EXECUTION_ARGS_CODE,
    UserConfig,
    argutil,
    checks,
    fileutil,
    gen_parser,
    generator,
    jdata,
    review,
    setup_check,
)
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.verified import accept, exempt, lookup, records, snapshot
from surf_scoter_lib.verified import previous as previous_acceptance

# The generator refuses a review file larger than this, too.
MAX_REVIEW_BYTES = 2 * 1024 * 1024
REVIEW_FILE_NAME = "review.yaml"
PROMPT = "Use these settings, edit them, or deny them? [u]se / [e]dit / [v]iew / [D]eny: "
# How long the setup's `runs: current-branch` may take.
BRANCH_TIMEOUT_SECONDS = 30


class Terminal(enum.Enum):
    """Whether `run_verify` can ask the user about the review at a terminal."""

    # It can when both the input and the output are a terminal.
    DETECT = "detect"
    AVAILABLE = "available"
    ABSENT = "absent"


class SetupNotes(enum.Enum):
    """Whether the review lists how the user's setup changes the defaults' protections."""

    INCLUDE = "include"
    # Leave them out (`--skip-setup-review`).
    SKIP = "skip"


class AcceptVia(enum.Enum):
    """How `run_verify` comes to an acceptance."""

    # The user reviews the project's settings.
    REVIEW = "review"
    # The suggestion is accepted as it stands, under the branch's exemption
    # (`--with-exemption`).
    EXEMPTION = "exemption"


class Platform(enum.Enum):
    """How the platform's shell splits a command line."""

    POSIX = "posix"
    WINDOWS = "windows"

    @staticmethod
    def current() -> "Platform":
        """This computer's."""
        return Platform.WINDOWS if fileutil.is_windows() else Platform.POSIX


def run_verify(
    config: UserConfig,
    output: Path | None,
    *,
    terminal: Terminal = Terminal.DETECT,
    setup_notes: SetupNotes = SetupNotes.INCLUDE,
    via: AcceptVia = AcceptVia.REVIEW,
) -> int:
    """Write the review file to `output`; or, without one, ask about it at the terminal.

    With `SetupNotes.INCLUDE`, the review lists what the user should know
    about their setup's host command checks, as `scoter user-setup` does.
    With `AcceptVia.EXEMPTION`, the project's suggestion is accepted as it
    stands, without asking, under the exemption that holds for the branch
    (see `accept_with_exemption`).
    """
    if config.project_setup() is None:
        print("No project here: run this in a project, or give --project-dir.", file=sys.stderr)
        return 1
    if via == AcceptVia.EXEMPTION:
        return accept_with_exemption(config)
    if output is None:
        interactive = (
            argutil.can_ask() if terminal == Terminal.DETECT else terminal == Terminal.AVAILABLE
        )
        if not interactive:
            print(
                "The review needs a terminal to ask you about it; nothing was accepted.  "
                "Give --output to only write the review file.",
                file=sys.stderr,
            )
            return 1
    now = datetime.datetime.now(datetime.UTC)
    branch = current_branch(config)
    taken: snapshot.Snapshot | None
    if output is None:
        try:
            taken = lookup.project_snapshot(config, Path(os.path.realpath(project_dir(config))))
        except (OSError, SurfScoterErr) as err:
            print(f"Couldn't read the project's files: {review.clean(str(err))}", file=sys.stderr)
            return 1
    else:
        taken = review.project_snapshot(config)
    report = checks.check_project(config)
    context = review.Context(
        report=report, now=now, files=taken, branch=branch, accepted_note=accepted_note(config),
        previous=find_previous(config, branch),
        setup_notes=setup_check.setup_report(config) if setup_notes == SetupNotes.INCLUDE else None,
    )
    text = review.build(config, context)
    if output is not None:
        write_private(output, text)
        print(f"Wrote the review to {review.clean(str(output))}", file=sys.stderr)
        print(summary(report), file=sys.stderr)
        return 0
    assert taken is not None  # nosec: B101  # narrowing for the type checker
    return Prompt(config, taken, branch, suggested=review.suggested(config, report)).run(text)


def accept_with_exemption(config: UserConfig, now: datetime.datetime | None = None) -> int:
    """Accept the project's suggestion as it stands, under its exemption, without a prompt.

    Only while an exemption (`scoter exempt`) holds for the branch, and only
    when the checks refuse nothing: the user doesn't see the review, so
    nothing they'd have to fix can be in it.  The new acceptance (kind
    `exempted`) has the review's usual expiry; the exemption moves to it,
    with the same end, so the run picks up the project file and
    `dev-env.yaml` changes it ignored.
    """
    now = now or records.now_utc()
    try:
        exemption = exempt.current(config, now)
    except (OSError, ValueError, SurfScoterErr) as err:
        print(f"Couldn't read your exemption: {review.clean(str(err))}", file=sys.stderr)
        return 1
    if exemption is None:
        print(
            "No exemption holds for this project here: start one with `scoter exempt`, "
            "or review the settings with `scoter verify`.",
            file=sys.stderr,
        )
        return 1
    branch = current_branch(config)
    try:
        taken = lookup.project_snapshot(config, Path(os.path.realpath(project_dir(config))))
    except (OSError, SurfScoterErr) as err:
        print(f"Couldn't read the project's files: {review.clean(str(err))}", file=sys.stderr)
        return 1
    report = checks.check_project(config)
    text = review.build(config, review.Context(report=report, now=now, files=taken, branch=branch))
    prompt = Prompt(config, taken, branch, suggested=review.suggested(config, report))
    directory = Path(tempfile.mkdtemp(prefix="scoter-review-"))
    try:
        data = text.encode("ascii")
        checked = prompt.check(data, directory, directory / REVIEW_FILE_NAME)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    # The review leaves out what the checks refuse; the suggestion must have nothing refused.
    if report.refused or not checked.usable:
        prompt.show(checked if not report.refused else Checked(report=report))
        print(
            "The project's settings can't be accepted without a review; nothing was accepted.  "
            "Run `scoter verify` to review them.",
            file=sys.stderr,
        )
        return 1
    assert checked.accepted is not None  # nosec: B101  # narrowing for the type checker
    try:
        acceptance = accept.store_acceptance(
            config, taken, data, checked.accepted, branch, now,
            suggested=prompt.suggested, exemption=exemption,
        )
    except (OSError, SurfScoterErr, KeyError, TypeError, ValueError) as err:
        print(f"Couldn't store your acceptance: {review.clean(str(err))}", file=sys.stderr)
        return 1
    print(summary(checked.report), file=sys.stderr)
    print(
        f"Accepted the project's settings as they stand ({acceptance.id}), under your exemption "
        f"until {exemption.until.isoformat()}.",
        file=sys.stderr,
    )
    return 0


def project_dir(config: UserConfig) -> Path:
    """The run's project directory."""
    return config.require_active_location("project-dir")


def summary(report: checks.Report) -> str:
    """How many refusals and warnings."""
    warned = sum(1 for f in report.findings if f.severity == checks.Severity.WARN)
    return f"{len(report.refused)} refused, {warned} warnings."


# ---------------------------------------------------------------------
# The prompt


class ReviewFileErr(SurfScoterErr):
    """The review file can't be read safely: it was replaced, say."""


@dataclass
class Checked:
    """A review file, parsed and checked."""

    # The generator's complaints: the file isn't what it can read.
    problems: list[str] = field(default_factory=list)
    report: checks.Report = field(default_factory=checks.Report)
    rejected: bool = False
    accepted: jdata.DictJsonData | None = None

    @property
    def usable(self) -> bool:
        """Whether it can be stored."""
        return (
            not self.problems and not self.report.refused
            and not self.rejected and self.accepted is not None
        )


@dataclass
class Prompt:
    """Asks the user to use, edit or deny a review file, checking it each time."""

    config: UserConfig
    taken: snapshot.Snapshot
    branch: str | None
    # The project's suggestion as the review wrote it, stored for the next
    # review to compare with.
    suggested: jdata.DictJsonData | None = None
    ask: Callable[[str], str] = input
    # Runs the editor on the review file: (file, the directory to start in).
    edit: Callable[[Path, Path], None] | None = None
    # Shows the review file's text: (the text, the directory to start in).
    page: Callable[[str, Path], None] | None = None
    # Parses a review file: (the file, a private directory) -> the generator's
    # universal file with its `review`, or its complaints.
    parse: Callable[[Path, Path], tuple[jdata.DictJsonData | None, list[str]]] | None = None
    now: Callable[[], datetime.datetime] = records.now_utc

    def run(self, text: str) -> int:
        """Ask about `text`; 0 when the user used it and it's stored."""
        directory = Path(tempfile.mkdtemp(prefix="scoter-review-"))
        try:
            return self._run(directory, text)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def _run(self, directory: Path, text: str) -> int:
        fd, name = tempfile.mkstemp(suffix="-" + REVIEW_FILE_NAME, dir=directory)
        with os.fdopen(fd, "w", encoding="ascii") as out:
            out.write(text)
        path = Path(name)
        data = text.encode("ascii")
        checked = self.check(data, directory, path)
        self.show_review(data, directory)
        while True:
            self.show(checked)
            choice = self.choose()
            if choice == "deny":
                print("Denied: nothing was accepted.", file=sys.stderr)
                return 1
            if choice == "view":
                self.show_review(data, directory)
                continue
            if choice == "edit":
                try:
                    self.run_editor(path, directory)
                    data = read_once(path)
                except ReviewFileErr as err:
                    print(f"{review.clean(str(err))}; nothing was accepted.", file=sys.stderr)
                    return 1
                checked = self.check(data, directory, path)
                continue
            if checked.rejected:
                print("The review says `result: reject`: nothing was accepted.", file=sys.stderr)
                return 1
            if not checked.usable:
                print("These settings can't be used as they are; edit or deny them.", file=sys.stderr)
                continue
            assert checked.accepted is not None  # nosec: B101  # narrowing for the type checker
            try:
                acceptance = accept.store_acceptance(
                    self.config, self.taken, data, checked.accepted, self.branch, self.now(),
                    suggested=self.suggested,
                )
            except (OSError, SurfScoterErr, KeyError, TypeError, ValueError) as err:
                print(f"Couldn't store your acceptance: {review.clean(str(err))}", file=sys.stderr)
                return 1
            print(
                f"Accepted ({acceptance.id}), until {acceptance.expires_at.isoformat()}.",
                file=sys.stderr,
            )
            return 0

    def show_review(self, data: bytes, directory: Path) -> None:
        """Show the review file as it is now: through the pager, or printed."""
        text = data.decode("utf-8", errors="replace")
        if self.page is not None:
            self.page(text, directory)
        else:
            page(text, directory)

    def choose(self) -> str:
        """`use`, `edit`, `view` or `deny`; Enter, end of input or Ctrl-C deny."""
        while True:
            try:
                answer = self.ask(PROMPT).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print(file=sys.stderr)
                return "deny"
            if answer in ("", "d", "deny", "n", "no"):
                return "deny"
            if answer in ("u", "use"):
                return "use"
            if answer in ("e", "edit"):
                return "edit"
            if answer in ("v", "view"):
                return "view"

    def show(self, checked: Checked) -> None:
        """What stops the file being used, or what's worth knowing about it."""
        for problem in checked.problems:
            print(f"  error: {review.clean(problem)}", file=sys.stderr)
        for finding in checked.report.findings:
            print(
                f"  {finding.severity}: {review.clean(finding.key)}: {review.clean(finding.message)}",
                file=sys.stderr,
            )
        if checked.rejected:
            print("The review says `result: reject`.", file=sys.stderr)
        elif not checked.usable:
            print("These settings can't be used until they're fixed.", file=sys.stderr)
        else:
            print(summary(checked.report), file=sys.stderr)

    def run_editor(self, path: Path, directory: Path) -> None:
        """Edit the review file."""
        if self.edit is not None:
            self.edit(path, directory)
        else:
            run_editor(path, directory)

    def check(self, data: bytes, directory: Path, shown: Path) -> Checked:
        """Parse the review file's bytes with the generator, then run the checks on them.

        `shown` is the file the user edits, which the complaints name.
        """
        # The generator reads its own private copy, so what's checked is what's stored.
        fd, name = tempfile.mkstemp(suffix="-" + REVIEW_FILE_NAME, dir=directory)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        try:
            parse = self.parse or (lambda file, where: parse_review(self.config, file, where))
            suc, problems = parse(Path(name), directory)
        finally:
            os.unlink(name)
        problems = [problem.replace(name, str(shown)) for problem in problems]
        if suc is None:
            return Checked(problems=problems or ["the review file couldn't be read"])
        parsed = UserConfig(suc)
        try:
            if parsed.review_result() == "reject":
                return Checked(rejected=True)
            report = checks.check_review(parsed)
        except SurfScoterErr as err:
            return Checked(problems=[str(err)])
        return Checked(report=report, accepted=parsed.review_accepted())


def split_command(command: str, platform: Platform) -> list[str]:
    """A command from the environment (`EDITOR`, `PAGER`), split as its shell would."""
    if platform == Platform.POSIX:
        return shlex.split(command)
    # Windows paths are full of backslashes, which POSIX splitting would eat.
    return [
        part[1:-1] if len(part) >= 2 and part[0] == part[-1] == '"' else part
        for part in shlex.split(command, posix=False)
    ]


def page(text: str, directory: Path) -> None:
    """Show `text` through `PAGER` when it's set and the output is a terminal; else print it.

    The pager starts in `directory` (the review's private one), as the editor does.
    """
    command = os.environ.get("PAGER")
    if command and sys.stdout.isatty():
        args = split_command(command, Platform.current())
        try:
            subprocess.run(  # nosec: B603  # Your own pager, from your environment.
                args, input=text.encode("utf-8"), cwd=directory, check=False, shell=False,
            )
            return
        except OSError as err:
            print(f"Couldn't run your pager ({args[0]}): {err}", file=sys.stderr)
    sys.stdout.write(text)


def editor_command(environ: Mapping[str, str], platform: Platform) -> list[str]:
    """The editor to run: `VISUAL`, or else `EDITOR`; on Windows, or else Notepad."""
    command = environ.get("VISUAL") or environ.get("EDITOR")
    if command:
        return split_command(command, platform)
    if platform == Platform.WINDOWS:
        # By its full path, so nothing named `notepad` elsewhere is picked up.
        return [os.path.join(environ.get("SystemRoot", "C:\\Windows"), "System32", "notepad.exe")]
    raise ReviewFileErr("Set VISUAL or EDITOR to edit the review")


def run_editor(path: Path, directory: Path) -> None:
    """Run the editor (`editor_command`) on the file, in `directory`, not the project.

    Editors read configuration from the directory they start in (vim's
    `.exrc`, an editor's workspace settings), so not the project's.
    """
    args = [*editor_command(os.environ, Platform.current()), str(path)]
    try:
        subprocess.run(  # nosec: B603  # Your own editor, from your environment.
            args, cwd=directory, check=False, shell=False,
        )
    except OSError as err:
        raise ReviewFileErr(f"Couldn't run your editor ({args[0]}): {err}") from err


def read_once(path: Path) -> bytes:
    """The review file's bytes, read once, refusing a link or anything but your own regular file."""
    flags = os.O_RDONLY | fileutil.O_NOFOLLOW | fileutil.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except OSError as err:
        if os.path.islink(path):
            raise ReviewFileErr(f"{path}: was replaced by a link") from err
        raise ReviewFileErr(f"{path}: {err.strerror}") from err
    with os.fdopen(fd, "rb") as inp:
        st = os.fstat(inp.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ReviewFileErr(f"{path}: isn't a regular file any more")
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            raise ReviewFileErr(f"{path}: belongs to someone else now")
        if st.st_nlink != 1:
            raise ReviewFileErr(f"{path}: has other links to it now")
        data = inp.read(MAX_REVIEW_BYTES + 1)
    if len(data) > MAX_REVIEW_BYTES:
        raise ReviewFileErr(f"{path}: larger than {MAX_REVIEW_BYTES} bytes")
    return data


def parse_review(
    config: UserConfig, file: Path, directory: Path,
) -> tuple[jdata.DictJsonData | None, list[str]]:
    """Run the generator with the review file: its universal file, or its complaints."""
    args = ["--project-dir", str(project_dir(config))]
    vcs_root = config.active_location("vcs-root")
    if vcs_root is not None:
        args.extend(("--vcs-root", str(vcs_root)))
    args.extend(("--review", str(file)))
    return generator.run(config, args, directory)


# ---------------------------------------------------------------------
# The review's context


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
            args, capture_output=True, check=False, shell=False, timeout=BRANCH_TIMEOUT_SECONDS,
            env=config.command_env(section),
        )
    except (OSError, subprocess.SubprocessError, SurfScoterErr):
        return None
    branch = res.stdout.decode("utf-8", errors="replace").strip()
    return branch if res.returncode == 0 and branch else None


def find_previous(config: UserConfig, branch: str | None) -> previous_acceptance.Previous | None:
    """The earlier acceptance to compare with, with what the user accepted; None for a full review.

    The user's `review.yaml` is read by the generator (`--previous-review`).
    If anything about it can't be read, the review is a full one.
    """
    try:
        found = previous_acceptance.find(config, branch)
    except (OSError, ValueError, SurfScoterErr) as err:
        print(f"Your earlier acceptances couldn't be read ({review.clean(str(err))}); "
              "this is a full review.", file=sys.stderr)
        return None
    if found is None:
        return None
    args = ["--project-dir", str(project_dir(config))]
    vcs_root = config.active_location("vcs-root")
    if vcs_root is not None:
        args.extend(("--vcs-root", str(vcs_root)))
    args.extend(("--previous-review", str(found.review_file)))
    directory = Path(tempfile.mkdtemp(prefix="scoter-previous-"))
    try:
        suc, problems = generator.run(config, args, directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    data = jdata.find_dict(suc, "previous-review", "data") if suc else None
    if data is None:
        print(
            f"Your earlier review ({found.acceptance.id}) couldn't be read "
            f"({review.clean('; '.join(problems))}); this is a full review.",
            file=sys.stderr,
        )
        return None
    found.accepted = data
    return found


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
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | fileutil.O_NOFOLLOW
    fd = os.open(path, flags, fileutil.MODE_USER_RW)
    with os.fdopen(fd, "w", encoding="ascii") as out:
        out.write(text)


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "Review the project's settings: they're resolved, checked, and shown with what "
        "the checks found, for you to use, edit or deny.  Using them stores your acceptance."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE
    parser.add_argument(
        "-o",
        "--output",
        action="store",
        dest="output",
        default=None,
        help="Only write the review file here, without asking about it or accepting anything.",
    )
    parser.add_argument(
        "--skip-setup-review",
        action="store_false",
        dest="setup_review",
        default=True,
        help=(
            "Leave out of the review what your setup changes about the host command checks "
            "(the checks it removes or replaces, and its overrides), which `scoter user-setup` shows."
        ),
    )
    parser.add_argument(
        "--with-exemption",
        action="store_true",
        dest="with_exemption",
        help=(
            "Under the exemption that holds for this branch (`scoter exempt`), accept the project's "
            "settings as they stand, without asking, when the checks refuse nothing.  Runs then "
            "pick up your changes to the project file and the `dev-env.yaml` files."
        ),
    )
    args = parser.parse_args(argv)
    if args.with_exemption and args.output:
        parser.error("--with-exemption accepts the settings; it can't be given with --output")
    return run_verify(
        config, Path(args.output) if args.output else None,
        setup_notes=SetupNotes.INCLUDE if args.setup_review else SetupNotes.SKIP,
        via=AcceptVia.EXEMPTION if args.with_exemption else AcceptVia.REVIEW,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv))
