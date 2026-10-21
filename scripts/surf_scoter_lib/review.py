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
import json
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import checks, gateway, jdata
from .user_config import UserConfig
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
    """A new acceptance's expiry: `verified-expiry-days` from now (30 by default)."""
    days = config.optional_int_value("scoter", "verified-expiry-days") or 30
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
    report = context.report
    out = Writer()
    _header(out, config, context)
    out.data(f"review: {scalar(FORMAT)}")
    out.data("result: reject", off=True)
    project = config.active_location("project-dir")
    out.data(f"project-dir: {scalar(os.path.realpath(project) if project else '')}")
    out.data(f"expires-at: {scalar(expires_at(config, context.now))}")
    out.data("")
    _egress(out, config, report.findings)
    _env(out, config)
    _commands(out, config, report)
    _dev_container(out, config, context)
    return out.text()


def _header(out: Writer, config: UserConfig, context: Context) -> None:
    findings = context.report.findings
    project = config.active_location("project-dir")
    out.note(f"Surf Scoter review: {project}")
    files_dir = config.optional_value("scoter", "project-files-dir") or ""
    snapshot_id = f" (snapshot {context.files.manifest.id})" if context.files else ""
    out.note(f"  project files  {files_dir}{snapshot_id}")
    if context.branch:
        out.note(f"  branch         {context.branch}")
    out.note(f"  gateway        {config.optional_value('network-gateway', 'name') or 'none'}")
    if context.accepted_note:
        out.note(f"  {context.accepted_note}")
    out.note()
    counts = {s: sum(1 for f in findings if f.severity == s) for s in checks.Severity}
    out.note(
        f"Summary: {counts[checks.Severity.REFUSE]} refused (commented out below), "
        f"{counts[checks.Severity.WARN]} warnings, {counts[checks.Severity.NOTE]} notes."
    )
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


def _egress(out: Writer, config: UserConfig, findings: Sequence[checks.Finding]) -> None:
    project = config.project_setup() or {}
    entries = [jdata.req_dict(project, *k) for k in jdata.find_list_keys(project, "egress")]
    try:
        rules = gateway.rules_of(entries)
    except gateway.RuleErr as err:
        out.note(f"The project's egress rules can't be read: {err}")
        out.data("egress: []")
        out.data("")
        return
    if not rules:
        out.data("egress: []")
        out.data("")
        return
    out.data("egress:")
    for i, rule in enumerate(rules):
        out.notes((_finding_text(f) for f in _findings_on(findings, f"egress[{i}]")), indent=2)
        entry: dict[str, jdata.JsonData] = {
            "host": rule.host, "protocol": rule.protocol, "port": rule.port,
        }
        if rule.methods:
            entry["methods"] = list(rule.methods)
        out.data("- " + flow(entry), indent=2)
    out.data("")


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


def _has_env(plan: jdata.DictJsonData | None) -> bool:
    return plan is not None and any(
        jdata.find(plan, part) for part in ("allow", "drop", "error", "set", "default")
    )


def _env(out: Writer, config: UserConfig) -> None:
    plan = config.raw("current-project-resolved", "env")
    plan = plan if isinstance(plan, dict) else None
    if not _has_env(plan):
        out.data("env: {}")
        out.data("")
        return
    out.data("env:")
    _plan_lines(out, plan, jdata.find_dict(config.project_setup(), "env"), indent=2)
    out.data("")


def _commands(out: Writer, config: UserConfig, report: checks.Report) -> None:
    commands = config.project_commands()
    if not commands:
        out.data("commands: []")
        out.data("")
        return
    resolved = {r.index: r for r in report.resolved}
    out.data("commands:")
    for i, command in enumerate(commands):
        at = f"commands[{i}]"
        found = _findings_on(report.findings, at)
        refusals = [f for f in found if f.severity == checks.Severity.REFUSE]
        policy = sorted({c for f in refusals for c in f.checks})
        safety = [f for f in refusals if not f.checks]
        out.notes((_finding_text(f) for f in found), indent=2)
        r = resolved.get(i)
        if r and r.executable:
            how = "through your setup's `command-map`" if r.source == "command-map" else "found on the PATH"
            out.note(f"runs {r.executable} ({how})", indent=2)
        off = bool(refusals)
        if refusals and safety:
            out.note("This can't be accepted: it's refused for a reason your setup can't set aside.", indent=2)
        elif refusals:
            out.note("To accept it for this project anyway, uncomment it, keeping `$override`.", indent=2)
        program = r.executable if r and r.executable else (r.name if r else "")
        out.data(f"- command: {scalar(jdata.req_str(command, 'command'))}", 2, off)
        out.data(f"program: {scalar(program)}", 4, off)
        out.data(f"args: {flow(jdata.req_list(command, 'args'))}", 4, off)
        cwd = jdata.find_dict(command, "cwd")
        if cwd is not None:
            out.data(f"cwd: {flow(cwd)}", 4, off)
        plan = config.raw("current-project-resolved", "commands", i, "env")
        plan = plan if isinstance(plan, dict) else None
        if _has_env(plan):
            out.data("env:", 4, off)
            _plan_lines(out, plan, jdata.find_dict(command, "env"), 6, off)
        if refusals and not safety:
            out.data(f"$override: {flow(policy)}", 4, off)
    out.data("")


def _dev_container(out: Writer, config: UserConfig, context: Context) -> None:
    out.data("dev-container:")
    container = config.active_location("dev-container-path")
    files_dir_name = config.optional_value("scoter", "project-files-dir") or ""
    project = config.active_location("project-dir")
    files_dir = Path(os.path.realpath(project)) / files_dir_name if project else None
    out.note(f"The project's container: {container}", indent=2)
    project_file = config.optional_value("scoter", "project-file-name")
    if context.files:
        for rel, entry in sorted(context.files.manifest.files.items()):
            if rel == project_file:
                continue
            extra = "  executable" if entry.executable else ""
            out.note(f"  {rel}  {entry.size} bytes  sha256:{entry.sha256[:12]}...{extra}", indent=2)
            if snapshot.is_dockerfile(rel.rsplit("/", 1)[-1]):
                text = context.files.contents[rel].decode("utf-8", errors="replace")
                for line in text.splitlines():
                    out.note(f"    | {line}", indent=2)
            for finding in _findings_on(context.report.findings, rel):
                out.note(f"    {_finding_text(finding)}", indent=2)
    else:
        out.note("  (its files couldn't be read; see the findings above)", indent=2)
    builds = config.dev_container_parent_builds() if config.has_dev_container() else []
    for tag, directory in builds:
        real = Path(os.path.realpath(directory))
        whose = "the project's" if files_dir and real.is_relative_to(files_dir) else "your installation's, trusted"
        out.note(f"  parent {tag}: {directory} ({whose})", indent=2)
    out.note("To build one of your own containers instead, set `use: {name: NAME}`.", indent=2)
    out.data('use: "project"', indent=2)
    images = _images(context, [tag for tag, _ in builds])
    if not images:
        out.data("images: {}", indent=2)
        return
    out.note("Each image the build starts from, and what to build from instead: pin one to", indent=2)
    out.note("a digest, or point it at an image you already have.", indent=2)
    out.data("images:", indent=2)
    for image in images:
        if "@sha256:" not in image:
            out.note("warn: no digest; the tag can point at new content at any time.", indent=4)
        out.data(f"{scalar(image)}: {scalar(image)}", indent=4)


def _images(context: Context, local_tags: Sequence[str]) -> list[str]:
    """The images the project's container directories build from, in order, each once."""
    if not context.files:
        return []
    images: list[str] = []
    for rel, data in sorted(context.files.contents.items()):
        if snapshot.is_dockerfile(rel.rsplit("/", 1)[-1]):
            for image in checks.from_images(data.decode("utf-8", errors="replace"), local_tags):
                if image not in images and "$" not in image:
                    images.append(image)
    return images
