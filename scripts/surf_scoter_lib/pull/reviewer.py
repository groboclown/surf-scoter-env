#SPDX:MIT
"""The default reviewer: a tree explorer in the terminal, over the analysis report.

The porcelain (`scoter pull`) runs the setup's `pull: runs: reviewer` with
its own SUC, the working directory's id and a session file.  The session
file holds the porcelain's options (`surf-scoter-pull-session-1`), and the
reviewer replaces it with its decision: `approve` and the analysis id it
showed, or `deny`.  The decision is only a request: `approve` checks
everything again (VER-28).

The tree: the root summary, then the findings, the host commands, the
egress rules, the dev container's values, the changed files, and, folded,
the unchanged files and the findings no longer found (VER-41).  A file shows
its contents through `review.file_lines`, and how it differs from the
configuration in use.  Everything shown from the project is a report's
`clean` text, or file contents made `visible` (VER-26).

The loop (VER-28): before every prompt, `scoter pull changed` says whether
the working directory changed since the analysis on screen; if it did, it's
analyzed again, and the new report compared with the last by finding id:
`new` findings are marked, `same` ones keep what the developer did, and
resolved ones fold into "no longer found".  Approval is always of the
analysis on screen.
"""
import difflib
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TextIO

from .. import fileutil, jdata, review, subcommand
from ..err import SurfScoterErr
from ..user_config import UserConfig
from ..verified import configs, lookup, snapshot, store
from . import seed, workdir

SESSION_SCHEMA = "surf-scoter-pull-session-1"
APPROVE = "approve"
DENY = "deny"
# The word the developer types to approve; anything else, or Enter, denies.
APPROVE_WORD = "approve"


# ---------------------------------------------------------------------
# The session file


@dataclass
class Session:
    """The porcelain's options for the reviewer, and the reviewer's decision."""

    # Files over the limits, or not UTF-8, that the developer allows unseen.
    allow_opaque: list[str] = field(default_factory=list)
    # Finding ids an earlier review already showed (`scoter review`).
    known_findings: list[str] = field(default_factory=list)
    result: str | None = None
    analysis: str | None = None

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data."""
        return {
            "$schema": SESSION_SCHEMA,
            "allow-opaque": list(self.allow_opaque),
            "known-findings": list(self.known_findings),
            "result": self.result,
            "analysis": self.analysis,
        }

    @staticmethod
    def from_json(data: jdata.JsonData) -> "Session":
        """From JSON data."""
        if jdata.find_str(data, "$schema") != SESSION_SCHEMA:
            raise workdir.PullErr(f"not a `{SESSION_SCHEMA}` session file")
        result = jdata.find_str(data, "result")
        if result not in (None, APPROVE, DENY):
            raise workdir.PullErr(f"the reviewer's result {result!a} isn't `approve` or `deny`")
        analysis = jdata.find_str(data, "analysis")
        return Session(
            allow_opaque=jdata.find_str_list(data, "allow-opaque"),
            known_findings=jdata.find_str_list(data, "known-findings"),
            result=result,
            analysis=None if analysis is None else workdir.check_analysis_id(analysis),
        )


def write_session(path: Path, session: Session) -> None:
    """Replace the session file with `session`, private to the user."""
    text = json.dumps(session.to_json(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    fileutil.write_private(path.parent, [path.name], text.encode("ascii"))


def read_session(path: Path) -> Session:
    """The session file at `path`."""
    try:
        fd = os.open(path, os.O_RDONLY | fileutil.O_NOFOLLOW | fileutil.O_BINARY)
        with os.fdopen(fd, "rb") as inp:
            data = json.loads(inp.read(1024 * 1024).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as err:
        raise workdir.PullErr(f"{path}: the reviewer's session file can't be read ({err})") from err
    try:
        return Session.from_json(data)
    except (KeyError, TypeError) as err:
        raise workdir.PullErr(f"{path}: {err}") from err


# ---------------------------------------------------------------------
# The phases, and comparing analyses


class Phases(Protocol):
    """How the reviewer asks for an analysis, and whether anything changed since one."""

    def analyze(self, workdir_id: str) -> jdata.DictJsonData:
        """A new analysis of the working directory."""
        ...

    def changed(self, workdir_id: str, analysis_id: str) -> jdata.DictJsonData:
        """What changed since the analysis."""
        ...


@dataclass(frozen=True)
class SubcommandPhases:
    """The phases, run as `scoter pull analyze` and `changed`, with the SUC the reviewer was given."""

    config: UserConfig

    def _run(self, args: Sequence[str]) -> jdata.DictJsonData:
        finished = subcommand.run(self.config, "pull", args, capture=True)
        if finished.code != 0 or finished.stdout is None:
            raise workdir.PullErr(f"`scoter pull {args[0]}` failed")
        value = json.loads(finished.stdout)
        if not isinstance(value, dict):
            raise workdir.PullErr(f"`scoter pull {args[0]}` printed something unexpected")
        return value

    def analyze(self, workdir_id: str) -> jdata.DictJsonData:
        return self._run(["analyze", workdir_id])

    def changed(self, workdir_id: str, analysis_id: str) -> jdata.DictJsonData:
        return self._run(["changed", workdir_id, analysis_id])


@dataclass
class Comparison:
    """How a report's findings compare with the last report's, by id."""

    new: set[str]
    same: set[str]
    # The last report's findings missing from this one.
    resolved: list[jdata.DictJsonData]


def compare(
    previous: jdata.DictJsonData | None, current: jdata.DictJsonData, known: set[str],
) -> Comparison:
    """`current`'s findings against `previous`'s: `new`, `same` or resolved.

    Without a previous report, a finding is `same` only when it's in `known`
    (an earlier review showed it).
    """
    now = {jdata.req_str(f, "id"): f for f in _findings(current)}
    before = {jdata.req_str(f, "id"): f for f in _findings(previous)} if previous else {}
    seen = set(before) | known
    return Comparison(
        new={i for i in now if i not in seen},
        same={i for i in now if i in seen},
        resolved=[f for i, f in before.items() if i not in now],
    )


def _findings(report: jdata.DictJsonData | None) -> list[jdata.DictJsonData]:
    return [jdata.req_dict(report, *k) for k in jdata.find_list_keys(report or {}, "findings")]


def clean_of(value: jdata.JsonData) -> str:
    """The `clean` form of a project text field; a generated value as it is."""
    if isinstance(value, dict):
        return jdata.req_str(value, "clean")
    return "" if value is None else review.clean(str(value))


# ---------------------------------------------------------------------
# The explorer


class Done(Exception):
    """The developer decided."""

    def __init__(self, result: str, analysis: str | None = None) -> None:
        super().__init__(result)
        self.result = result
        self.analysis = analysis


@dataclass
class Reviewer:
    """The terminal explorer, for one working directory."""

    phases: Phases
    workdir_id: str
    files_dir: Path
    session: Session
    ask: Callable[[str], str]
    out: TextIO
    max_file_size: int = snapshot.SnapshotLimits().max_file_size
    # The contents of a file in the configuration in use, by path; None when it has none.
    current_file: Callable[[str], bytes | None] = lambda _: None
    report: jdata.DictJsonData = field(default_factory=dict)
    comparison: Comparison | None = None
    opened: set[str] = field(default_factory=set)
    changed_files: int = 0

    # ---- the loop

    @property
    def analysis_id(self) -> str:
        """The analysis on screen."""
        return jdata.req_str(self.report, "id")

    def reanalyze(self) -> None:
        """Analyze again, and compare with the last report by finding id."""
        report = self.phases.analyze(self.workdir_id)
        self.comparison = compare(self.report or None, report, set(self.session.known_findings))
        self.report = report

    def notice_changes(self) -> bool:
        """Re-analyze if the working directory changed since the analysis on screen; whether it did."""
        changed = self.phases.changed(self.workdir_id, self.analysis_id)
        paths = [p for key in ("added", "removed", "changed") for p in jdata.find_str_list(changed, key)]
        host_file = changed.get("host-file") is True
        if not paths and not host_file:
            return False
        self.changed_files = len(paths) + (1 if host_file else 0)
        self.say(f"{self.changed_files} file(s) changed since the last analysis; analyzing again.")
        self.reanalyze()
        assert self.comparison is not None  # nosec: B101  # set by reanalyze
        self.say(
            f"  {len(self.comparison.new)} new finding(s), "
            f"{len(self.comparison.resolved)} no longer found."
        )
        return True

    def run(self) -> Session:
        """Explore until the developer approves or denies; the session, with the decision."""
        self.reanalyze()
        try:
            self.menu()
        except Done as done:
            self.session.result = done.result
            self.session.analysis = done.analysis
        return self.session

    # ---- input and output

    def say(self, line: str = "") -> None:
        """Print a line of generated or cleaned text."""
        self.out.write(line + "\n")

    def prompt(self, text: str) -> str:
        """Ask, after checking for changes; end of input denies."""
        self.notice_changes()
        try:
            return self.ask(text).strip()
        except EOFError as err:
            raise Done(DENY) from err

    # ---- the tree

    def counts(self) -> jdata.DictJsonData:
        return jdata.req_dict(self.report, "root", "counts")

    def is_new(self, finding_id: str) -> bool:
        return self.comparison is not None and finding_id in self.comparison.new

    def menu(self) -> None:
        """The root."""
        while True:
            root = jdata.req_dict(self.report, "root")
            files = _list(self.report, "files")
            changed = [f for f in files if f.get("base") != "same" or f.get("current") not in (None, "same") or f.get("findings")]
            unchanged = [f for f in files if f not in changed]
            new = len(self.comparison.new) if self.comparison else 0
            resolved = self.comparison.resolved if self.comparison else []
            self.say()
            self.say(f"Analysis {self.analysis_id}: {jdata.req_str(root, 'summary')}")
            if self.changed_files:
                self.say(f"  {self.changed_files} file(s) changed since the analysis before this one.")
            if root.get("changed-since-base") is not None:
                self.say(f"  {root['changed-since-base']} file(s) added or changed since the last pull.")
            removed = jdata.find_list(root, "removed-since-base") or []
            if removed:
                self.say(f"  {len(removed)} file(s) removed since the last pull: "
                         + ", ".join(clean_of(r) for r in removed))
            for line in jdata.find_str_list(self.report, "recommendations"):
                self.say(f"  - {line}")
            entries: list[tuple[str, Callable[[], None]]] = [
                (f"Findings ({len(_list(self.report, 'findings'))}, {new} new)", self.show_findings),
                (f"Host commands ({len(_list(self.report, 'host-commands'))})", self.show_commands),
                (f"Egress rules ({len(_list(self.report, 'egress'))})", self.show_egress),
                ("Dev container values", self.show_dev_container),
                (f"Changed, new or flagged files ({len(changed)})", lambda c=changed: self.show_files(c)),
                (f"Unchanged files ({len(unchanged)})", lambda u=unchanged: self.show_files(u)),
            ]
            if resolved:
                entries.append((f"No longer found ({len(resolved)})", self.show_resolved))
            self.say()
            for i, (label, _) in enumerate(entries, 1):
                self.say(f"  {i}. {label}")
            self.say("  r. Analyze again   a. Approve   q. Deny")
            choice = self.prompt("> ")
            if choice == "q":
                raise Done(DENY)
            if choice == "a":
                self.approve()
            elif choice == "r":
                self.reanalyze()
            elif choice.isdigit() and 1 <= int(choice) <= len(entries):
                entries[int(choice) - 1][1]()

    def finding_line(self, finding: jdata.DictJsonData) -> str:
        mark = "*" if self.is_new(jdata.req_str(finding, "id")) else " "
        return (
            f" {mark} [{jdata.req_str(finding, 'severity')}] {clean_of(finding['key'])}: "
            f"{clean_of(finding['message'])}"
        )

    def findings_by_id(self, ids: Sequence[str]) -> list[jdata.DictJsonData]:
        wanted = set(ids)
        return [f for f in _list(self.report, "findings") if f.get("id") in wanted]

    def show_findings(self) -> None:
        self.say("Findings (* new since the last analysis):")
        for finding in _list(self.report, "findings"):
            self.say(self.finding_line(finding))
        self.pause()

    def show_resolved(self) -> None:
        assert self.comparison is not None  # nosec: B101  # only offered with one
        self.say("No longer found:")
        for finding in self.comparison.resolved:
            self.say("   " + self.finding_line(finding).strip(" *"))
        self.pause()

    def show_commands(self) -> None:
        for command in _list(self.report, "host-commands"):
            path = command.get("path")
            where = clean_of(path) if path else "not on this computer"
            note = " (not in the host file yet: approval resolves it)" if command.get("unresolved") else ""
            self.say(
                f"  {command.get('index')}. {clean_of(command['command'])}: runs "
                f"{clean_of(command['program'])} as {where} [{clean_of(command.get('origin'))}]{note}"
            )
            for finding in self.findings_by_id(jdata.find_str_list(command, "findings")):
                self.say("    " + self.finding_line(finding))
        self.pause()

    def show_egress(self) -> None:
        for rule in _list(self.report, "egress"):
            self.say(f"  {rule.get('index')}. {clean_of(rule['host'])}")
            for finding in self.findings_by_id(jdata.find_str_list(rule, "findings")):
                self.say("    " + self.finding_line(finding))
        self.pause()

    def show_dev_container(self) -> None:
        for group, label in (("values", "The dev container"), ("project", "The project file's `container`")):
            self.say(f"{label}:")
            for key, value in sorted((jdata.find_dict(self.report, "dev-container", group) or {}).items()):
                self.say(f"  {review.clean(key)}: {clean_of(value)}")
        for container in _list(self.report, "containers"):
            where = "in the configuration" if container.get("in-configuration") else "yours, outside it"
            self.say(f"  container {review.clean(str(container.get('path')))} ({where})")
        self.pause()

    def show_files(self, files: list[jdata.DictJsonData]) -> None:
        while True:
            for i, entry in enumerate(files, 1):
                path = clean_of(entry["path"])
                marks = [m for m in (entry.get("base"), entry.get("current")) if m and m != "same"]
                opened = "" if jdata.req_str(entry["path"], "text") in self.opened else " (not opened)"
                self.say(f"  {i}. {path} {'/'.join(str(m) for m in marks)}{opened}")
            choice = self.prompt("Open which file (Enter goes back)? ")
            if not choice.isdigit() or not 1 <= int(choice) <= len(files):
                return
            self.show_file(files[int(choice) - 1])

    def show_file(self, entry: jdata.DictJsonData) -> None:
        path = jdata.req_str(entry["path"], "text")
        self.opened.add(path)
        self.say(f"{clean_of(entry['path'])}: {entry.get('size')} bytes"
                 + (", executable" if entry.get("executable") else ""))
        for note in jdata.find_str_list(entry, "annotations"):
            self.say(f"  - {note}")
        for finding in self.findings_by_id(jdata.find_str_list(entry, "findings")):
            self.say(self.finding_line(finding))
        text = self.read_text(path)
        if text is None:
            self.say("  Not shown: it's over the size limit, or not UTF-8 text.  Approving needs "
                     "`--allow-opaque` naming it.")
        else:
            for n, line in enumerate(review.file_lines(text), 1):
                self.say(f"{n:5}  {line}")
            current = self.current_file(path)
            if current is not None:
                diff = list(difflib.unified_diff(
                    review.file_lines(current.decode("utf-8", errors="replace")),
                    review.file_lines(text), "your current configuration", "this pull", lineterm="",
                ))
                if diff:
                    self.say("How it differs from your current configuration (apply your changes again by hand):")
                    for line in diff:
                        self.say("  " + line)
        self.pause()

    def read_text(self, path: str) -> str | None:
        """The working directory's file at `path`, as text; None when it's opaque."""
        snapshot.check_name(path)
        snapshot.check_relative(path)
        try:
            fd = os.open(self.files_dir / path, os.O_RDONLY | fileutil.O_NOFOLLOW | fileutil.O_BINARY)
            with os.fdopen(fd, "rb") as inp:
                data = inp.read(self.max_file_size + 1)
        except OSError as err:
            self.say(f"  It can't be read: {review.clean(str(err))}")
            return None
        if len(data) > self.max_file_size:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def pause(self) -> None:
        self.prompt("(Enter goes back) ")

    # ---- approving

    def opaque(self) -> list[str]:
        """The files that can't be shown, and aren't allowed unseen."""
        allowed = set(self.session.allow_opaque)
        return [
            jdata.req_str(f["path"], "text") for f in _list(self.report, "files")
            if jdata.req_str(f["path"], "text") not in allowed
            and self.read_text(jdata.req_str(f["path"], "text")) is None
        ]

    def approve(self) -> None:
        """Approve the analysis on screen, if it still holds and nothing refuses."""
        if self.notice_changes():
            self.say("The analysis changed; look it over before approving.")
            return
        if self.counts().get("refuse"):
            self.say(f"It can't be approved: {self.counts()['refuse']} finding(s) refuse it.")
            return
        opaque = self.opaque()
        if opaque:
            self.say("These files can't be shown, so they can't be approved unseen; pull again with "
                     "`--allow-opaque PATH` for each one you accept:")
            for path in opaque:
                self.say(f"  {review.clean(path)}")
            return
        unopened = [
            clean_of(f["path"]) for f in _list(self.report, "files")
            if (f.get("base") in ("added", "changed") or f.get("findings"))
            and jdata.req_str(f["path"], "text") not in self.opened
        ]
        if unopened:
            self.say("You haven't opened these new, changed or flagged files:")
            for path in unopened:
                self.say(f"  {path}")
        answer = self.ask(f"Type `{APPROVE_WORD}` to approve analysis {self.analysis_id}; Enter denies: ")
        if answer.strip() != APPROVE_WORD:
            raise Done(DENY)
        raise Done(APPROVE, self.analysis_id)


def _list(data: jdata.DictJsonData, key: str) -> list[jdata.DictJsonData]:
    return [jdata.req_dict(data, *k) for k in jdata.find_list_keys(data, key)]


def review_working_dir(
    config: UserConfig, workdir_id: str, session_path: Path, ask: Callable[[str], str], out: TextIO,
) -> Session:
    """Run the default reviewer on the working directory; the session, with the decision written."""
    wd = workdir.open_workdir(config, workdir_id)
    session = read_session(session_path)
    session.result = session.analysis = None
    verified_root = config.optional_path("verification", "store")

    def current_file(path: str) -> bytes | None:
        if verified_root is None or not verified_root.is_dir():
            return None
        verified = store.VerifiedStore.open(verified_root)
        link = configs.load_link(verified, wd.provenance.project_dir, wd.provenance.env_id)
        in_use = None if link is None or link.in_use is None else configs.load_config(verified, link.id, link.in_use)
        entry = None if in_use is None else in_use.snapshot.files.get(path)
        return None if entry is None else verified.get_blob(entry.sha256, entry.size)

    seed.require_found(config)
    reviewer = Reviewer(
        SubcommandPhases(config), workdir_id, wd.files, session, ask, out,
        max_file_size=lookup.limits(config).max_file_size, current_file=current_file,
    )
    try:
        decided = reviewer.run()
    except SurfScoterErr:
        session.result = DENY
        write_session(session_path, session)
        raise
    write_session(session_path, decided)
    return decided
