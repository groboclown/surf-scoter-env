#SPDX:MIT
"""Writing the review file: the project's suggestion, for the user to accept, edit or reject.

The file's layout and rules are in step 12 of docs/contributors/projects-v1.md:
only what becomes the accepted settings is YAML data; everything the user
needs to judge it is a comment.  Explanations start `#scoter:`; what the
user might switch on (`result: reject`, a refused command) is YAML
commented out with `# `.

It's written without a YAML library, as a small subset the generator's
parser reads (step 13): every string as JSON writes it, with
`ensure_ascii`, so bidirectional, zero-width and look-alike characters show
as `\\u` escapes; placeholders and short lists in flow style; no anchors,
tags or block scalars.  Comments are cleaned the same way, with control
characters (terminal escape sequences included) taken out, one line each.
"""
import datetime
import difflib
import json
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import checks, gateway, jdata
from .user_config import UserConfig
from .verified import previous as previous_acceptance
from .verified import snapshot

FORMAT = "surf-scoter-review-1"

# Terminal escape sequences: CSI (`ESC [ ... final`), OSC (`ESC ] ... BEL` or
# `ESC ] ... ESC \\`), and two-character escapes.
_ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_PLAIN_KEY = re.compile(r"\$?[A-Za-z][A-Za-z0-9_-]*")

_SEVERITY_ORDER = {checks.Severity.REFUSE: 0, checks.Severity.WARN: 1, checks.Severity.NOTE: 2}


# ---------------------------------------------------------------------
# Text


def clean(text: str) -> str:
    """`text` for one comment line, or the terminal.

    Terminal escape sequences and other control characters go (line breaks
    and tabs become spaces), and anything outside ASCII shows as a `\\u`
    escape, so nothing hides, or poses as something else.
    """
    text = _ESCAPES.sub("", text)
    text = re.sub(r"[\r\n\t]", " ", text)
    out: list[str] = []
    for char in text:
        code = ord(char)
        if code < 0x20 or 0x7F <= code < 0xA0:
            continue
        if code < 0x7F:
            out.append(char)
        elif code <= 0xFFFF:
            out.append(f"\\u{code:04x}")
        else:
            out.append(f"\\U{code:08x}")
    return "".join(out)


def scalar(value: jdata.JsonData) -> str:
    """A scalar as YAML: text exactly as JSON writes it (ASCII, escaped), the rest as JSON."""
    return json.dumps(value, ensure_ascii=True)


def key(name: str) -> str:
    """A mapping key: plain when it's a simple name, else as JSON writes it."""
    return name if _PLAIN_KEY.fullmatch(name) else scalar(name)


def flow(value: jdata.JsonData) -> str:
    """A value on one line: `{k: v}` and `[a, b]`."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{key(k)}: {flow(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(flow(v) for v in value) + "]"
    return scalar(value)


@dataclass
class Writer:
    """Lines of the review file."""

    lines: list[str] = field(default_factory=list)

    def note(self, text: str = "", indent: int = 0) -> None:
        """A `#scoter:` comment."""
        self.lines.append(" " * indent + ("#scoter: " + clean(text)).rstrip())

    def notes(self, texts: Iterable[str], indent: int = 0) -> None:
        for text in texts:
            self.note(text, indent)

    def data(self, line: str, indent: int = 0, off: bool = False) -> None:
        """A line of data; with `off`, commented out with `# `."""
        prefix = " " * indent
        self.lines.append(f"{prefix}# {line}" if off else f"{prefix}{line}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


# ---------------------------------------------------------------------
# The pieces


@dataclass(frozen=True)
class Context:
    """What the review is made from, besides the universal configuration file."""

    report: checks.Report
    now: datetime.datetime
    # The project's files, for the dev container's listing; None if they couldn't be read.
    files: snapshot.Snapshot | None = None
    branch: str | None = None
    # A line about an acceptance that already matches, if any.
    accepted_note: str | None = None
    # The earlier acceptance to compare with (step 17), with what the user
    # accepted filled in; None for a full review.
    previous: previous_acceptance.Previous | None = None
    # How the user's setup changes the defaults' protections
    # (`setup_check.setup_report`); None to leave it out.
    setup_notes: Sequence[str] | None = None


def _findings_on(findings: Sequence[checks.Finding], at: str) -> list[checks.Finding]:
    """The findings about the item at `at` (`commands[0]`, `egress[1]`), worst first."""
    found = [f for f in findings if f.key == at or f.key.startswith((at + ".", at + "["))]
    return sorted(found, key=lambda f: _SEVERITY_ORDER[f.severity])


def _finding_text(finding: checks.Finding) -> str:
    text = f"{finding.severity}: {finding.message}"
    if finding.checks:
        text += " (" + ", ".join(finding.checks) + ")"
    return text


def _is_item(key: str) -> bool:
    return key.startswith(("commands[", "egress[", "env"))


def expires_at(config: UserConfig, now: datetime.datetime) -> str:
    """A new acceptance's expiry: `verification: expiry-days` from now (30 by default)."""
    days = config.optional_int_value("verification", "expiry-days") or 30
    return (now + datetime.timedelta(days=days)).replace(microsecond=0).isoformat()


def project_snapshot(config: UserConfig) -> snapshot.Snapshot | None:
    """The project's files, for the dev container's listing; None if they can't be read."""
    project = config.active_location("project-dir")
    files_dir = config.optional_value("scoter", "project-files-dir")
    file_name = config.optional_value("scoter", "project-file-name")
    if project is None or files_dir is None or file_name is None:
        return None
    parents = [d for _, d in config.dev_container_parent_builds()] if config.has_dev_container() else []
    dirs = snapshot.container_dirs(config.active_location("dev-container-path"), parents)
    try:
        return snapshot.snapshot_project(
            Path(os.path.realpath(project)) / files_dir, file_name, dirs,
            snapshot.ParsedFiles(config.project_setup(), config.dev_container()),
            snapshot.SnapshotLimits(),
        )
    except (OSError, ValueError, snapshot.SnapshotErr):
        return None


def build(config: UserConfig, context: Context) -> str:
    """The review file for the project in the universal configuration file."""
    view = suggested(config, context.report)
    out = Writer()
    _header(out, config, context)
    out.data(f"review: {scalar(FORMAT)}")
    out.data("result: reject", off=True)
    project = config.active_location("project-dir")
    out.data(f"project-dir: {scalar(os.path.realpath(project) if project else '')}")
    out.data(f"expires-at: {scalar(expires_at(config, context.now))}")
    accepted = context.previous.accepted if context.previous else None
    if accepted and isinstance(accepted.get("$comment"), str):
        out.data(f"$comment: {scalar(accepted['$comment'])}")
    out.data("")
    _egress(out, config, context, view)
    _env(out, config, context, view)
    _commands(out, config, context, view)
    _dev_container(out, config, context)
    return out.text()


def changes(config: UserConfig, context: Context) -> dict[str, int]:
    """How many items of each kind of change (step 17), for the summary."""
    previous = context.previous
    if previous is None:
        return {}
    view = suggested(config, context.report)
    counts: dict[str, int] = {}
    found = [
        *(("egress", c) for c in compare(
            "egress", _list(previous.suggested, "egress"),
            [_egress_entry(e) for e in _list(previous.accepted, "egress")], _list(view, "egress"),
        )),
        *(("commands", c) for c in compare(
            "commands", _list(previous.suggested, "commands"),
            [_command_entry(c) for c in _list(previous.accepted, "commands")], _list(view, "commands"),
        )),
    ]
    for section, change in found:
        kind = change.kind
        if kind == "removed-by-you" and any(
            f.severity == checks.Severity.REFUSE
            for f in _findings_on(context.report.findings, f"{section}[{change.index}]")
        ):
            # Refused, so not the user's removal: shown as the project's, refused (see `show`).
            continue
        counts[kind] = counts.get(kind, 0) + 1
    old_env = _plan(previous.suggested.get("env"))
    if old_env != _plan(view.get("env")):
        counts["changed"] = counts.get("changed", 0) + 1
    if context.files is not None:
        project_file = config.optional_value("scoter", "project-file-name")
        now_files = {r: d for r, d in context.files.contents.items() if r != project_file}
        before = {r: d for r, d in previous.files.items() if r != project_file}
        for kind, number in (
            ("file-changed", sum(1 for r, d in now_files.items() if r in before and before[r] != d)),
            ("file-added", sum(1 for r in now_files if r not in before)),
            ("file-removed", sum(1 for r in before if r not in now_files)),
        ):
            if number:
                counts[kind] = number
    return counts


def _removed_notes(config: UserConfig, context: Context) -> list[tuple[str, str]]:
    """The user's `$comment`s on items the project removed since: (`egress` or `commands`, the note)."""
    previous = context.previous
    if previous is None:
        return []
    view = suggested(config, context.report)
    found: list[tuple[str, str]] = []
    for section, entry in (("egress", _egress_entry), ("commands", _command_entry)):
        for change in compare(
            section, _list(previous.suggested, section),
            [entry(e) for e in _list(previous.accepted, section)], _list(view, section),
        ):
            if change.kind == "removed" and change.mine and isinstance(change.mine.get("$comment"), str):
                found.append((section, change.mine["$comment"]))
    return found


_CHANGE_WORDS = (
    ("changed", "changed since"), ("added", "new"), ("removed", "removed by the project"),
    ("edited", "your changes kept"), ("yours", "added by you"), ("removed-by-you", "removed by you"),
    ("file-changed", "container files changed"), ("file-added", "container files new"),
    ("file-removed", "container files removed"),
)


def _header(out: Writer, config: UserConfig, context: Context) -> None:
    findings = context.report.findings
    project = config.active_location("project-dir")
    out.note(f"Surf Scoter review: {project}")
    files_dir = config.optional_value("scoter", "project-files-dir") or ""
    snapshot_id = f" (snapshot {context.files.manifest.id})" if context.files else ""
    out.note(f"  project files  {files_dir}{snapshot_id}")
    if context.branch:
        out.note(f"  branch         {context.branch}")
    out.note(f"  gateway        {config.optional_value('dev-env', 'network-gateway-name') or 'none'}")
    if context.accepted_note:
        out.note(f"  {context.accepted_note}")
    previous = context.previous
    if previous is not None:
        acceptance = previous.acceptance
        state = "expired" if acceptance.expires_at <= context.now else "expires"
        branch = f"branch {acceptance.branch}, " if acceptance.branch else ""
        out.note(
            f"  compared with your acceptance {acceptance.id} ({branch}{state} "
            f"{acceptance.expires_at.isoformat()}): only what changed since is shown in full."
        )
        for change in previous.setup_changes:
            out.note(f"  your setup changed since: {change}")
        for section, item in _removed_notes(config, context):
            out.note(
                f"  your note on an item the project removed ({section}; below, commented out, "
                f"and it goes with the item unless you keep it): {item}"
            )
    if context.setup_notes is not None:
        out.note()
        if context.setup_notes:
            out.note("How your setup changes the defaults' protections (as `scoter user-setup` shows):")
            out.notes(f"  {note}" for note in context.setup_notes)
        else:
            out.note("Your setup changes none of the defaults' protections.")
    out.note()
    counts = {s: sum(1 for f in findings if f.severity == s) for s in checks.Severity}
    out.note(
        f"Summary: {counts[checks.Severity.REFUSE]} refused (commented out below), "
        f"{counts[checks.Severity.WARN]} warnings, {counts[checks.Severity.NOTE]} notes."
    )
    if previous is not None:
        kinds = changes(config, context)
        listed = [f"{kinds[k]} {words}" for k, words in _CHANGE_WORDS if kinds.get(k)]
        out.note("Since you accepted it: " + (", ".join(listed) if listed else "nothing changed") + ".")
    for finding in sorted(findings, key=lambda f: _SEVERITY_ORDER[f.severity]):
        if finding.severity != checks.Severity.NOTE:
            out.note(f"  {finding.severity:<7} {finding.key}: {finding.message}")
    for finding in findings:
        if finding.severity == checks.Severity.NOTE and not _is_item(finding.key):
            out.note(f"  note    {finding.key}: {finding.message}")
    out.note()
    out.notes([
        "To say no, remove the `# ` before `result: reject`.  To accept less, delete an",
        "egress rule or a command, narrow a command's arguments, or shorten `expires-at`.",
        "You may add commands and egress rules too; the next review shows them as yours.",
        "To keep a note with this review, add a `$comment:` to any entry; `#` comments",
        "aren't kept.  `#scoter:` lines are written afresh by each review.",
    ])


# ---------------------------------------------------------------------
# The data, as the review writes it


_PLAN_PARTS = ("allow", "drop", "error", "set", "default")


def _strip(item: jdata.DictJsonData | None) -> jdata.DictJsonData | None:
    """`item` without its `$comment` and `$override`: what's compared."""
    return None if item is None else {k: v for k, v in item.items() if not k.startswith("$")}


def _plan(plan: jdata.JsonData) -> jdata.DictJsonData:
    """An `env` block's non-empty parts, as the review writes them."""
    if not isinstance(plan, dict):
        return {}
    return {part: plan[part] for part in _PLAN_PARTS if plan.get(part)}


def _egress_entry(entry: jdata.DictJsonData) -> jdata.DictJsonData:
    """An egress rule as the review writes it: its port worked out, `methods` only when set."""
    marks = {k: v for k, v in entry.items() if k.startswith("$")}
    try:
        [rule] = gateway.rules_of([_strip(entry) or {}])
    except (gateway.RuleErr, ValueError):
        return entry
    out: jdata.DictJsonData = {"host": rule.host, "protocol": rule.protocol, "port": rule.port}
    if rule.methods:
        out["methods"] = list(rule.methods)
    return {**out, **marks}


def _command_entry(entry: jdata.DictJsonData) -> jdata.DictJsonData:
    """A command as the review writes it, in a fixed order, `env` only when set."""
    out: jdata.DictJsonData = {
        "command": entry.get("command"), "program": entry.get("program"), "args": entry.get("args"),
    }
    if entry.get("cwd") is not None:
        out["cwd"] = entry["cwd"]
    env = _plan(entry.get("env"))
    if env:
        out["env"] = env
    for mark in ("$override", "$comment"):
        if mark in entry:
            out[mark] = entry[mark]
    return out


def suggested(config: UserConfig, report: checks.Report) -> jdata.DictJsonData:
    """The project's suggestion as the review writes it: `egress`, `env` and `commands`.

    An acceptance stores it (`suggestion.json`), so the next review can
    compare what the project suggests then with what it suggested now.
    """
    project = config.project_setup() or {}
    entries = [jdata.req_dict(project, *k) for k in jdata.find_list_keys(project, "egress")]
    try:
        egress = [_egress_entry(e) for e in entries] if gateway.rules_of(entries) else []
    except gateway.RuleErr:
        egress = []
    resolved = {r.index: r for r in report.resolved}
    commands: list[jdata.JsonData] = []
    for i, command in enumerate(config.project_commands()):
        r = resolved.get(i)
        entry: jdata.DictJsonData = {
            "command": jdata.req_str(command, "command"),
            "program": r.executable if r and r.executable else (r.name if r else ""),
            "args": jdata.req_list(command, "args"),
            "cwd": jdata.find_dict(command, "cwd"),
            "env": config.raw("current-project-resolved", "commands", i, "env"),
        }
        commands.append(_command_entry(entry))
    return {
        "egress": egress,
        "env": _plan(config.raw("current-project-resolved", "env")),
        "commands": commands,
    }


# ---------------------------------------------------------------------
# Comparing with the earlier acceptance (step 17)


@dataclass(frozen=True)
class Change:
    """One item, compared across the three versions.

    `kind`: `fresh` (no earlier acceptance), `unchanged`, `edited` (the user
    changed it), `removed-by-you`, `changed` (it changed since), `added`
    (new since), `removed` (gone since), or `yours` (the user added it).
    """

    kind: str
    old: jdata.DictJsonData | None
    mine: jdata.DictJsonData | None
    new: jdata.DictJsonData | None
    # The item's index in the current suggestion, for its findings.
    index: int | None


def _key(section: str, item: jdata.DictJsonData) -> object:
    if section == "egress":
        return (item.get("host"), item.get("protocol"), item.get("port"))
    return item.get("command")


def _pairs(section: str, a: Sequence[jdata.DictJsonData], b: Sequence[jdata.DictJsonData]) -> dict[int, int]:
    """Which item of `b` each item of `a` is (by index).

    Egress rules match by host, protocol and port.  Commands match by name
    when it's on each side once; otherwise only an identical one matches.
    """
    pairs: dict[int, int] = {}
    taken: set[int] = set()
    a_keys = [_key(section, x) for x in a]
    b_keys = [_key(section, x) for x in b]
    for i, item in enumerate(a):
        k = a_keys[i]
        if section == "egress" or (a_keys.count(k) == 1 and b_keys.count(k) == 1):
            j = next((j for j, bk in enumerate(b_keys) if bk == k and j not in taken), None)
        else:
            j = next((j for j, other in enumerate(b) if j not in taken and _strip(other) == _strip(item)), None)
        if j is not None:
            pairs[i] = j
            taken.add(j)
    return pairs


def compare(
    section: str,
    old: Sequence[jdata.DictJsonData] | None,
    mine: Sequence[jdata.DictJsonData] | None,
    new: Sequence[jdata.DictJsonData],
) -> list[Change]:
    """Each item of a list (`egress` or `commands`) across the three versions.

    Without an earlier acceptance (`old` and `mine` None), every item is fresh.
    """
    if old is None or mine is None:
        return [Change("fresh", None, None, n, i) for i, n in enumerate(new)]
    old_new = _pairs(section, old, new)
    old_mine = _pairs(section, old, mine)
    mine_new = _pairs(section, mine, new)
    new_old = {j: i for i, j in old_new.items()}
    mine_paired = set(old_mine.values())
    new_mine = {j: i for i, j in mine_new.items() if i not in mine_paired}
    changes: list[Change] = []
    for j, n in enumerate(new):
        if j not in new_old:
            m = mine[new_mine[j]] if j in new_mine else None
            changes.append(Change("added", None, m, n, j))
            continue
        o = old[new_old[j]]
        m = mine[old_mine[new_old[j]]] if new_old[j] in old_mine else None
        if _strip(o) != _strip(n):
            kind = "changed"
        elif m is None:
            kind = "removed-by-you"
        else:
            kind = "unchanged" if _strip(m) == _strip(o) else "edited"
        changes.append(Change(kind, o, m, n, j))
    for i, o in enumerate(old):
        if i not in old_new and i in old_mine:
            changes.append(Change("removed", o, mine[old_mine[i]], None, None))
    used = mine_paired | set(new_mine.values())
    for i, m in enumerate(mine):
        if i not in used:
            changes.append(Change("yours", None, m, None, None))
    return changes


def differences(before: jdata.JsonData, after: jdata.JsonData) -> list[str]:
    """What differs between two versions of an item, a line for each key."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return [f"{flow(before)} -> {flow(after)}"]
    a, b = _strip(before) or {}, _strip(after) or {}
    return [
        f"  {k}: {flow(a[k]) if k in a else '(none)'} -> {flow(b[k]) if k in b else '(none)'}"
        for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)
    ]


@dataclass
class Shown:
    """How an item is written: its data, whether commented out, the notes before it,
    and an earlier version of the user's, written commented out after it."""

    data: jdata.DictJsonData
    off: bool
    notes: list[str]
    earlier: jdata.DictJsonData | None = None


def _without(item: jdata.DictJsonData, mark: str) -> jdata.DictJsonData:
    return {k: v for k, v in item.items() if k != mark}


def show(change: Change, found: Sequence[checks.Finding]) -> Shown | None:
    """How to write `change`, given the findings on its current version."""
    refusals = [f for f in found if f.severity == checks.Severity.REFUSE]
    policy = sorted({c for f in refusals for c in f.checks})
    safety = any(not f.checks for f in refusals)
    texts = [_finding_text(f) for f in found]
    if refusals and safety:
        guide = ["This can't be accepted: it's refused for a reason your setup can't set aside."]
    elif refusals:
        guide = ["To accept it for this project anyway, uncomment it, keeping `$override`."]
    else:
        guide = []

    def suggestion(new: jdata.DictJsonData) -> jdata.DictJsonData:
        item = _without(_without(new, "$override"), "$comment")
        return {**item, "$override": policy} if refusals and not safety else item

    new, mine = change.new, change.mine
    kind = change.kind
    if kind == "removed-by-you" and refusals:
        kind = "fresh"
    if kind in ("fresh", "added"):
        assert new is not None  # nosec: B101  # these have a current version
        lead = ["new: the project added this since you accepted it."] if kind == "added" else []
        return Shown(suggestion(new), bool(refusals), lead + texts + guide)
    if kind == "unchanged":
        assert new is not None and mine is not None  # nosec: B101
        if not refusals:
            return Shown(mine, False, ["unchanged since you accepted it."])
        overridden = set(mine.get("$override") or [])
        if not safety and set(policy) <= overridden:
            return Shown(mine, False, [*texts, f"you overrode {', '.join(policy)} here; unchanged since you accepted it."])
        return Shown(suggestion(new), True, [*texts, *guide, "You accepted it before; it's refused now."])
    if kind == "edited":
        assert mine is not None  # nosec: B101
        return Shown(mine, False, [
            "your change, kept (it's checked again when you use the review):",
            *differences(change.old, mine),
        ])
    if kind == "removed-by-you":
        assert new is not None  # nosec: B101
        return Shown(suggestion(new), True, ["you removed this before; remove the `# ` to take it back."])
    if kind == "changed":
        assert new is not None  # nosec: B101
        data = suggestion(new)
        differ = {
            k for k in set(_strip(change.old) or {}) | set(_strip(new) or {})
            if (_strip(change.old) or {}).get(k) != (_strip(new) or {}).get(k)
        }
        lead = (
            "found somewhere else since you accepted it (the project didn't change it):"
            if differ == {"program"} else "changed since you accepted it:"
        )
        notes = [lead, *differences(change.old, new), *texts, *guide]
        if mine is not None and "$comment" in mine:
            data = {**data, "$comment": mine["$comment"]}
            notes.append("its `$comment` is from the earlier version.")
        earlier = None
        if mine is not None and _strip(mine) != _strip(change.old):
            earlier = _without(mine, "$override")
        return Shown(data, bool(refusals), notes, earlier)
    if kind == "removed":
        assert mine is not None  # nosec: B101
        return Shown(mine, True, ["the project removed this since you accepted it; remove the `# ` to keep it anyway."])
    if kind == "yours":
        assert mine is not None  # nosec: B101
        return Shown(mine, False, ["added by you (it's checked again when you use the review)."])
    return None


# ---------------------------------------------------------------------
# The sections


def _egress(out: Writer, config: UserConfig, context: Context, view: jdata.DictJsonData) -> None:
    project = config.project_setup() or {}
    entries = [jdata.req_dict(project, *k) for k in jdata.find_list_keys(project, "egress")]
    try:
        gateway.rules_of(entries)
    except gateway.RuleErr as err:
        out.note(f"The project's egress rules can't be read: {err}")
        out.data("egress: []")
        out.data("")
        return
    previous = context.previous
    changes = compare(
        "egress",
        _list(previous.suggested, "egress") if previous else None,
        [_egress_entry(e) for e in _list(previous.accepted, "egress")] if previous else None,
        _list(view, "egress"),
    )
    if not changes:
        out.data("egress: []")
        out.data("")
        return
    out.data("egress:")
    for change in changes:
        found = _findings_on(context.report.findings, f"egress[{change.index}]") if change.index is not None else []
        shown = show(change, found)
        if shown is None:
            continue
        out.notes(shown.notes, indent=2)
        out.data("- " + flow(shown.data), indent=2, off=shown.off)
        if shown.earlier is not None:
            out.note("your version before, commented out:", indent=2)
            out.data("- " + flow(shown.earlier), indent=2, off=True)
    out.data("")


def _list(data: jdata.JsonData, name: str) -> list[jdata.DictJsonData]:
    items = data.get(name) if isinstance(data, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _plan_lines(
    out: Writer, plan: jdata.DictJsonData | None, raw: jdata.DictJsonData | None,
    indent: int, off: bool = False,
) -> None:
    """An `env` block's non-empty parts, resolved, with each `${...}` source as a comment."""
    if plan is None:
        return
    for part in ("allow", "drop", "error"):
        values = jdata.find_str_list(plan, part)
        if values:
            out.data(f"{part}: {flow(values)}", indent, off)
    for part in ("set", "default"):
        values = jdata.find_dict(plan, part) or {}
        if not values:
            continue
        out.data(f"{part}:", indent, off)
        for name, value in values.items():
            source = jdata.find_str(raw, part, name) if raw else None
            if source is not None and "${" in source:
                out.note(f"from `{source}`", indent + 2)
            out.data(f"{key(name)}: {scalar(value)}", indent + 2, off)


def _env_block(out: Writer, plan: jdata.DictJsonData, raw: jdata.DictJsonData | None, off: bool) -> None:
    if not plan:
        out.data("env: {}", off=off)
        return
    out.data("env:", off=off)
    _plan_lines(out, plan, raw, indent=2, off=off)


_ENV_LISTS = ("allow", "drop", "error")
_ENV_MAPS = ("set", "default")


def _entries(plan: jdata.DictJsonData) -> dict[tuple[str, str], jdata.JsonData]:
    """An `env` block's entries: each `allow`, `drop` and `error` name (as True),
    and each `set` and `default` variable's value."""
    out: dict[tuple[str, str], jdata.JsonData] = {}
    for part in _ENV_LISTS:
        for name in plan.get(part) or []:
            out[(part, str(name))] = True
    for part in _ENV_MAPS:
        values = plan.get(part)
        for name, value in (values.items() if isinstance(values, dict) else ()):
            out[(part, name)] = value
    return out


def _plan_of(entries: dict[tuple[str, str], jdata.JsonData]) -> jdata.DictJsonData:
    plan: dict[str, jdata.JsonData] = {}
    for (part, name), value in entries.items():
        if part in _ENV_LISTS:
            plan.setdefault(part, [])
            plan[part].append(name)  # type: ignore[union-attr]
        else:
            plan.setdefault(part, {})
            plan[part][name] = value  # type: ignore[index]
    return _plan(plan)


def _label(key: tuple[str, str], value: jdata.JsonData) -> str:
    part, name = key
    return f"{part}: {name}" if part in _ENV_LISTS else f"{part}: {name} = {flow(value)}"


@dataclass
class EnvMerge:
    """The top-level `env`, merged variable by variable (step 17)."""

    plan: jdata.DictJsonData
    # What changed since, what the user changed and kept, and the user's
    # earlier values the project's change replaces.
    changed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    earlier: dict[tuple[str, str], jdata.JsonData] = field(default_factory=dict)
    # The entries whose value is the user's (no `${...}` source to show).
    mine: set[tuple[str, str]] = field(default_factory=set)


def merge_env(
    old: jdata.DictJsonData, mine: jdata.DictJsonData, new: jdata.DictJsonData,
    raw_old: jdata.DictJsonData | None, raw_new: jdata.DictJsonData | None,
) -> EnvMerge:
    """The three versions of an `env` block, merged by entry, as items are.

    An entry the project didn't change keeps the user's value (or its
    absence); one it changed takes the project's, with the user's earlier
    value kept aside when they'd edited it.  `raw_old` and `raw_new` are the
    project file's `env` as written, to tell a change in the project from a
    `${...}` the setup now resolves differently.
    """
    o, m, n = _entries(old), _entries(mine), _entries(new)
    raw_o = _entries(raw_old) if raw_old is not None else None
    raw_n = _entries(raw_new or {})
    merged: dict[tuple[str, str], jdata.JsonData] = {}
    result = EnvMerge({})
    for key in [*n, *(k for k in m if k not in n), *(k for k in o if k not in n and k not in m)]:
        if key in merged:
            continue
        ov, mv, nv = o.get(key), m.get(key), n.get(key)
        if ov == nv:
            if mv is not None:
                merged[key] = mv
                result.mine.add(key)
            if mv != ov:
                result.kept.append(
                    f"  {_label(key, mv)} (yours)" if mv is not None else f"  {_label(key, ov)} (you removed it)"
                )
            continue
        if nv is not None:
            merged[key] = nv
        if ov is None:
            result.changed.append(f"  {_label(key, nv)} (new)")
        elif nv is None:
            result.changed.append(f"  {_label(key, ov)} (the project removed it)")
        elif raw_o is not None and raw_o.get(key) == raw_n.get(key) and isinstance(raw_n.get(key), str):
            result.changed.append(
                f"  {key[0]}: {key[1]}: your setup now gives {flow(nv)} (was {flow(ov)}), "
                f"from `{raw_n[key]}`"
            )
        else:
            result.changed.append(f"  {key[0]}: {key[1]}: {flow(ov)} -> {flow(nv)}")
        if mv != ov and mv is not None:
            result.earlier[key] = mv
    result.plan = _plan_of(merged)
    return result


def _env(out: Writer, config: UserConfig, context: Context, view: jdata.DictJsonData) -> None:
    new = _plan(view.get("env"))
    raw = jdata.find_dict(config.project_setup(), "env")
    previous = context.previous
    if previous is None:
        _env_block(out, new, raw, False)
        out.data("")
        return
    merge = merge_env(
        _plan(previous.suggested.get("env")),
        _plan(previous.accepted.get("env") if previous.accepted else None),
        new,
        jdata.find_dict(previous.project, "env") if previous.project is not None else None,
        raw,
    )
    if merge.changed:
        out.note("changed since you accepted it:")
        out.notes(merge.changed)
    if merge.kept:
        out.note("your changes, kept (they're checked again when you use the review):")
        out.notes(merge.kept)
    if not merge.changed and not merge.kept:
        out.note("unchanged since you accepted it.")
    sources = {
        part: {k: v for k, v in (raw or {}).get(part, {}).items() if (part, k) not in merge.mine}
        for part in _ENV_MAPS if isinstance((raw or {}).get(part), dict)
    }
    _env_block(out, merge.plan, sources, False)
    if merge.earlier:
        out.note("your values before, commented out:")
        _env_block(out, _plan_of(merge.earlier), None, True)
    out.data("")


def _command_lines(
    out: Writer, command: jdata.DictJsonData, raw: jdata.DictJsonData | None, off: bool,
) -> None:
    out.data(f"- command: {scalar(command.get('command'))}", 2, off)
    out.data(f"program: {scalar(command.get('program'))}", 4, off)
    out.data(f"args: {flow(command.get('args'))}", 4, off)
    if command.get("cwd") is not None:
        out.data(f"cwd: {flow(command['cwd'])}", 4, off)
    env = _plan(command.get("env"))
    if env:
        out.data("env:", 4, off)
        _plan_lines(out, env, raw, 6, off)
    if "$override" in command:
        out.data(f"$override: {flow(command['$override'])}", 4, off)
    if "$comment" in command:
        out.data(f"$comment: {scalar(command['$comment'])}", 4, off)


def _commands(out: Writer, config: UserConfig, context: Context, view: jdata.DictJsonData) -> None:
    previous = context.previous
    changes = compare(
        "commands",
        _list(previous.suggested, "commands") if previous else None,
        [_command_entry(c) for c in _list(previous.accepted, "commands")] if previous else None,
        _list(view, "commands"),
    )
    if not changes:
        out.data("commands: []")
        out.data("")
        return
    project_commands = config.project_commands()
    resolved = {r.index: r for r in context.report.resolved}
    out.data("commands:")
    for change in changes:
        i = change.index
        found = _findings_on(context.report.findings, f"commands[{i}]") if i is not None else []
        shown = show(change, found)
        if shown is None:
            continue
        out.notes(shown.notes, indent=2)
        from_new = shown.data is not change.mine
        r = resolved.get(i) if i is not None and from_new else None
        if r and r.executable:
            how = "through your setup's `command-map`" if r.source == "command-map" else "found on the PATH"
            out.note(f"runs {r.executable} ({how})", indent=2)
        raw = jdata.find_dict(project_commands[i], "env") if i is not None and from_new else None
        _command_lines(out, shown.data, raw, shown.off)
        if shown.earlier is not None:
            out.note("your version before, commented out:", indent=2)
            _command_lines(out, shown.earlier, None, True)
    out.data("")


def _dev_container(out: Writer, config: UserConfig, context: Context) -> None:
    out.data("dev-container:")
    container = config.active_location("dev-container-path")
    files_dir_name = config.optional_value("scoter", "project-files-dir") or ""
    project = config.active_location("project-dir")
    files_dir = Path(os.path.realpath(project)) / files_dir_name if project else None
    out.note(f"The project's container: {container}", indent=2)
    project_file = config.optional_value("scoter", "project-file-name")
    before = context.previous.files if context.previous else None
    if context.files:
        for rel, entry in sorted(context.files.manifest.files.items()):
            if rel == project_file:
                continue
            extra = "  executable" if entry.executable else ""
            data = context.files.contents[rel]
            old = before.get(rel) if before is not None else None
            if before is not None and old == data:
                out.note(f"  {rel}  unchanged since you accepted it{extra}", indent=2)
            else:
                state = "" if before is None else ("  changed since you accepted it" if old is not None else "  new since you accepted it")
                out.note(f"  {rel}  {entry.size} bytes  sha256:{entry.sha256[:12]}...{extra}{state}", indent=2)
                if old is not None:
                    out.notes(_file_diff(old, data), indent=2)
                elif snapshot.is_dockerfile(rel.rsplit("/", 1)[-1]):
                    text = data.decode("utf-8", errors="replace")
                    for line in text.splitlines():
                        out.note(f"    | {line}", indent=2)
            for finding in _findings_on(context.report.findings, rel):
                out.note(f"    {_finding_text(finding)}", indent=2)
        for rel in sorted(set(before or {}) - set(context.files.manifest.files)):
            if rel != project_file:
                out.note(f"  {rel}  removed since you accepted it", indent=2)
    else:
        out.note("  (its files couldn't be read; see the findings above)", indent=2)
    builds = config.dev_container_parent_builds() if config.has_dev_container() else []
    for tag, directory in builds:
        real = Path(os.path.realpath(directory))
        whose = "the project's" if files_dir and real.is_relative_to(files_dir) else "your installation's, trusted"
        out.note(f"  parent {tag}: {directory} ({whose})", indent=2)
    out.note("To build one of your own containers instead, set `use: {name: NAME}`.", indent=2)
    accepted = context.previous.accepted if context.previous else None
    chosen = jdata.find_dict(accepted, "dev-container") if accepted else None
    use = (chosen or {}).get("use", "project")
    if use != "project":
        out.note("your choice, kept from your earlier acceptance.", indent=2)
    out.data(f"use: {flow(use)}", indent=2)
    images, through_args = _images(context, [tag for tag, _ in builds])
    for rel, image in through_args:
        out.note(
            f"note: {rel} builds `FROM {image}`, an image named through an `ARG`: `images` can't "
            "map it, as it isn't known until the build.",
            indent=2,
        )
    if not images:
        out.data("images: {}", indent=2)
        return
    mapped = jdata.find_dict(chosen, "images") or {} if chosen else {}
    out.note("Each image the build starts from, and what to build from instead: pin one to", indent=2)
    out.note("a digest, or point it at an image you already have.", indent=2)
    out.data("images:", indent=2)
    for image in images:
        target = mapped.get(image, image)
        if context.previous is not None and image not in mapped:
            out.note("new since you accepted it.", indent=4)
        elif target != image:
            out.note("your choice, kept from your earlier acceptance.", indent=4)
        if "@sha256:" not in target:
            out.note("warn: no digest; the tag can point at new content at any time.", indent=4)
        out.data(f"{scalar(image)}: {scalar(target)}", indent=4)


# The most lines of a file's differences shown, and the largest file compared.
MAX_DIFF_LINES = 200
MAX_DIFF_BYTES = 256 * 1024


def _file_diff(old: bytes, new: bytes) -> list[str]:
    """The lines of a file's differences, for comments; or a line saying they can't be shown."""
    if len(old) > MAX_DIFF_BYTES or len(new) > MAX_DIFF_BYTES:
        return [f"    (too large to compare: {len(old)} bytes before, {len(new)} now)"]
    try:
        before, after = old.decode("utf-8"), new.decode("utf-8")
    except UnicodeDecodeError:
        return [f"    (not text: {len(old)} bytes before, {len(new)} now)"]
    lines = [
        f"    | {line}"
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=2)
        if not line.startswith(("---", "+++"))
    ]
    if len(lines) > MAX_DIFF_LINES:
        lines = [*lines[:MAX_DIFF_LINES], f"    ({len(lines) - MAX_DIFF_LINES} more lines)"]
    return lines


def _images(context: Context, local_tags: Sequence[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """The images the project's containers build from, each once; and each `FROM` that
    names its image through an `ARG` (as its Dockerfile, and the image as written)."""
    if not context.files:
        return [], []
    images: list[str] = []
    through_args: list[tuple[str, str]] = []
    for rel, data in sorted(context.files.contents.items()):
        if snapshot.is_dockerfile(rel.rsplit("/", 1)[-1]):
            for image in checks.from_images(data.decode("utf-8", errors="replace"), local_tags):
                if "$" in image:
                    through_args.append((rel, image))
                elif image not in images:
                    images.append(image)
    return images, through_args
