#SPDX:MIT
"""`scoter pull analyze`: check a working directory, and report on it.

`analyze` reads a registered working directory and its provenance, and
changes nothing but its own record.  It snapshots the working directory,
runs the generator and the checks on a staged copy of that snapshot (the
bytes it names), and writes a report (`surf-scoter-pull-analysis-1`) for
the reviewer.  It records only its id, the snapshot and the host file's
hash, beside the working directory, so `changed` and `approve` can check
against it.  It knows nothing of earlier analyses: comparing them is the
reviewer's (VER-28).

Every finding has an `id` that stays the same while the issue does: a hash
of the check (or, for a finding no setup check raised, its generated
message), its severity, the kind of subject, the subject's key and the
value that triggered it.  Identical findings on one subject are numbered in
order.

Text from the project appears only in `{"text": ..., "clean": ...}` fields,
`clean` made by `review.clean()`; everything else is generated.  The report
is written with `ensure_ascii`.  It's output, not authority: `approve` never
reads one.
"""
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .. import checks, host_file, jdata, review
from ..user_config import UserConfig
from ..verified import configs, lookup, snapshot, store
from ..verified.store import now_utc
from . import seed, workdir

REPORT_SCHEMA = "surf-scoter-pull-analysis-1"
# Limits on the report: its size, and the entries in each list.
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 10_000

_ITEM = re.compile(r"(commands|egress)\[(\d+)\](.*)")
# The dev container's values the review shows on their own (VER-3).
DEV_CONTAINER_KEYS = ("user", "mounts", "broker-bin", "sshd-port", "sshd-port-env", "parent-builds")


@dataclass
class Inspection:
    """A working directory as it is now, checked."""

    files: snapshot.Snapshot
    host_hash: str
    host_contents: jdata.DictJsonData
    staged: UserConfig
    resolved: list[checks.Resolved]
    report: checks.Report
    # The selection the build uses, with canonical hashes: what approval stores.
    build: snapshot.Snapshot
    # The container directories: relative to the configuration, or outside it.
    containers: list[jdata.JsonData]


def inspect(
    config: UserConfig, wd: workdir.WorkDir, *, generate: seed.Generate, path_env: str | None = None,
) -> Inspection:
    """Snapshot the working directory, and check that snapshot.

    The generator and the checks read a staged copy of the snapshot, so
    what's checked is exactly what the snapshot names.  Each command's
    program is the host file's; one it doesn't name (a command the developer
    added) is looked up as seeding would, and shows as unresolved.
    """
    if path_env is None:
        path_env = os.environ.get("PATH", "")
    found = seed.require_found(config)
    _, file_name = seed.files_location(config, found)
    limits = lookup.limits(config)
    files, host_hash = wd.snapshot(limits, file_name)
    host_contents = host_file.read(wd.host_file)
    programs = jdata.find_dict(host_contents, "programs") or {}
    with tempfile.TemporaryDirectory(dir=workdir.pull_root(config)) as tmp:
        staged = seed.stage(files, Path(tmp), limits)
        staged_config = seed.generate_on(config, found, staged, Path(tmp) / "generator", generate)
        seed.check_vcs(staged_config, found)
        resolved: list[checks.Resolved] = []
        for r in checks.resolve_programs(staged_config, path_env):
            entry = programs.get(r.name)
            if isinstance(entry, dict):
                path = jdata.req_str(entry, "path")
                present = os.path.isfile(path) and os.access(path, os.X_OK)
                resolved.append(checks.Resolved(
                    r.index, r.command, r.name, path if present else None,
                    "host-file:" + jdata.req_str(entry, "origin"),
                ))
            else:
                resolved.append(checks.Resolved(r.index, r.command, r.name, r.executable, "unresolved:" + r.source))
        report = checks.check_resolved(staged_config, resolved, path_env)
        parsed = snapshot.ParsedFiles(staged_config.project_setup(), staged_config.dev_container())
        build = snapshot.select_build(files, staged, file_name, lookup.container_dirs(staged_config), parsed)
        # The container directories, relative to the copy, while it's there.
        containers = _containers(staged_config, staged)
    return Inspection(files, host_hash, host_contents, staged_config, resolved, report, build, containers)


def _containers(staged: UserConfig, staged_dir: Path) -> list[jdata.JsonData]:
    real = Path(os.path.realpath(staged_dir))
    out: list[jdata.JsonData] = []
    dev = staged.active_location("dev-container-path")
    entries: list[tuple[str | None, Path | None]] = [(None, dev)]
    if staged.has_dev_container():
        entries.extend((tag, d) for tag, d in staged.dev_container_parent_builds())
    for tag, directory in entries:
        if directory is None:
            continue
        where = Path(os.path.realpath(directory))
        inside = where.is_relative_to(real)
        out.append({
            "path": where.relative_to(real).as_posix() if inside else str(where),
            "in-configuration": inside,
            "tag": tag,
        })
    return out


# ---------------------------------------------------------------------
# Finding ids


def _subject(finding: checks.Finding, staged: UserConfig, resolved: list[checks.Resolved]) -> tuple[str, str, str, str]:
    """(kind, key, the rest of the finding's key, value) for a finding's id."""
    match = _ITEM.fullmatch(finding.key)
    if match is None:
        if finding.key == "PATH":
            return ("setup", "PATH", "", "")
        return ("file", finding.key, "", "")
    index = int(match.group(2))
    if match.group(1) == "commands":
        r = next((r for r in resolved if r.index == index), None)
        return ("host-command", r.name if r else str(index), match.group(3), (r.executable or "") if r else "")
    egress = staged.project_egress()
    host = jdata.find_str(egress[index], "host") if index < len(egress) else None
    return ("egress", host or str(index), match.group(3), host or "")


def finding_ids(
    findings: list[checks.Finding], staged: UserConfig, resolved: list[checks.Resolved],
) -> list[str]:
    """Each finding's id, in order: the same while the issue is."""
    seen: dict[str, int] = {}
    ids: list[str] = []
    for finding in findings:
        kind, key, rest, value = _subject(finding, staged, resolved)
        what = ",".join(finding.checks) if finding.checks else "message:" + finding.message
        base = json.dumps([what, str(finding.severity), kind, key, rest, value], ensure_ascii=True)
        n = seen.get(base, 0)
        seen[base] = n + 1
        ids.append("f-" + hashlib.sha256(f"{base}#{n}".encode("ascii")).hexdigest()[:20])
    return ids


# ---------------------------------------------------------------------
# The report


def _text(value: str) -> jdata.DictJsonData:
    """Project text: as written, and cleaned for a terminal."""
    return {"text": value, "clean": review.clean(value)}


def _compare(entry: snapshot.FileEntry, other: snapshot.Manifest | None, path: str) -> str | None:
    if other is None:
        return None
    before = other.files.get(path)
    if before is None:
        return "added"
    same = before.sha256 == entry.sha256 and before.executable == entry.executable
    return "same" if same else "changed"


def build_report(
    analysis_id: str,
    wd: workdir.WorkDir,
    inspection: Inspection,
    base: snapshot.Manifest | None,
    current: snapshot.Manifest | None,
) -> jdata.DictJsonData:
    """The analysis report."""
    findings = inspection.report.findings
    ids = finding_ids(findings, inspection.staged, inspection.resolved)
    by_key: dict[str, list[str]] = {}
    for finding, fid in zip(findings, ids, strict=True):
        by_key.setdefault(finding.key, []).append(fid)

    def under(prefix: str) -> list[str]:
        return [fid for key, fids in by_key.items() if key == prefix or key.startswith((prefix + ".", prefix + "[")) for fid in fids]

    vcs = jdata.find_dict(wd.provenance.vcs, "files") or {}
    build_files = set(inspection.build.manifest.files)
    files: list[jdata.JsonData] = []
    for path, entry in sorted(inspection.files.manifest.files.items()):
        annotations: list[str] = []
        if path not in build_files:
            annotations.append("Not used by the build: it isn't kept when the configuration is approved.")
        compared_base = _compare(entry, base, path)
        if compared_base in ("added", "changed"):
            annotations.append(f"{compared_base.capitalize()} since the last pull.")
        status = jdata.find_str(vcs, path, "status")
        if status in ("modified", "untracked"):
            annotations.append(f"The VCS reports it {status}: nobody committed this version.")
        files.append({
            "path": _text(path),
            "sha256": entry.sha256,
            "size": entry.size,
            "executable": entry.executable,
            "base": compared_base,
            "current": _compare(entry, current, path),
            "vcs": vcs.get(path),
            "annotations": annotations,
            "findings": under(path),
        })
    removed = sorted(set(base.files) - set(inspection.files.manifest.files)) if base else []
    host_commands: list[jdata.JsonData] = []
    for r in inspection.resolved:
        unresolved = r.source.startswith("unresolved:")
        host_commands.append({
            "index": r.index,
            "command": _text(r.command),
            "program": _text(r.name),
            "path": None if r.executable is None else _text(r.executable),
            "origin": r.source.split(":", 1)[1] if ":" in r.source else r.source,
            "unresolved": unresolved,
            "findings": under(f"commands[{r.index}]"),
        })
    egress: list[jdata.JsonData] = [
        {"index": i, "host": _text(jdata.find_str(rule, "host") or ""), "findings": under(f"egress[{i}]")}
        for i, rule in enumerate(inspection.staged.project_egress())
    ]
    counts = {str(s): sum(1 for f in findings if f.severity == s) for s in checks.Severity}
    dev = inspection.staged.dev_container() or {}
    container = jdata.find_dict(inspection.staged.project_setup() or {}, "container") or {}
    recommendations: list[str] = []
    if counts["refuse"]:
        recommendations.append(
            "Some findings refuse the configuration: edit the working directory until none do, "
            "or deny the pull."
        )
    if any(h["unresolved"] for h in host_commands if isinstance(h, dict)):
        recommendations.append(
            "Some commands aren't in the host file; approval resolves them, and shows where they run."
        )
    if any(jdata.find_str(v, "status") in ("modified", "untracked") for v in vcs.values()):
        recommendations.append(
            "Some files aren't as committed: commit or review them before pulling, or pull from a commit."
        )
    changed = sum(1 for f in files if isinstance(f, dict) and f["base"] in ("added", "changed"))
    report: jdata.DictJsonData = {
        "$schema": REPORT_SCHEMA,
        "id": analysis_id,
        "workdir": wd.id,
        "root": {
            "summary": (
                f"{len(files)} files, {len(host_commands)} host commands, {len(egress)} egress rules; "
                f"{counts['refuse']} refused, {counts['warn']} warnings, {counts['note']} notes."
            ),
            "counts": counts,
            "changed-since-base": None if base is None else changed,
            "removed-since-base": [_text(p) for p in removed],
        },
        "containers": inspection.containers,
        "dev-container": {
            "values": {
                key: _text(review.scalar(dev[key])) for key in DEV_CONTAINER_KEYS if key in dev
            },
            "project": {key: _text(review.scalar(value)) for key, value in sorted(container.items())},
        },
        "files": files,
        "host-commands": host_commands,
        "egress": egress,
        "findings": [
            {
                "id": fid,
                "severity": str(f.severity),
                "key": _text(f.key),
                "message": _text(f.message),
                "checks": list(f.checks),
            }
            for f, fid in zip(findings, ids, strict=True)
        ],
        "recommendations": recommendations,
    }
    for name in ("files", "host-commands", "egress", "findings"):
        if len(report[name]) > MAX_ENTRIES:  # type: ignore[arg-type]
            raise workdir.PullErr(f"the analysis has more than {MAX_ENTRIES} {name}")
    if len(to_text(report)) > MAX_REPORT_BYTES:
        raise workdir.PullErr(f"the analysis is larger than {MAX_REPORT_BYTES} bytes")
    return report


def to_text(report: jdata.DictJsonData) -> str:
    """The report as JSON text: ASCII only, so nothing raw reaches a reviewer."""
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def current_manifest(config: UserConfig, env_id: str, project_dir: str) -> snapshot.Manifest | None:
    """The snapshot of the configuration in use (or unlocked from), if there is one."""
    root = config.optional_path("verification", "store")
    if root is None or not root.is_dir():
        return None
    verified = store.VerifiedStore.open(root)
    link = configs.load_link(verified, project_dir, env_id)
    # While unlocked, the configuration unlocked from is the developer's current one.
    current = None if link is None else (link.unlocked.from_id if link.unlocked else link.in_use)
    if current is None:
        return None
    in_use = configs.load_config(verified, env_id, current)
    return None if in_use is None else in_use.snapshot


def base_manifest(config: UserConfig, env_id: str, project_dir: str) -> snapshot.Manifest | None:
    """The base: the snapshot last pulled, if there is one."""
    root = config.optional_path("verification", "store")
    if root is None or not root.is_dir():
        return None
    link = configs.load_link(store.VerifiedStore.open(root), project_dir, env_id)
    return None if link is None else link.base


def analyze(
    config: UserConfig, workdir_id: str, *, generate: seed.Generate, path_env: str | None = None,
) -> jdata.DictJsonData:
    """Analyze the working directory `workdir_id`; the report."""
    wd = workdir.open_workdir(config, workdir_id)
    inspection = inspect(config, wd, generate=generate, path_env=path_env)
    analysis_id = workdir.new_analysis_id(now_utc())
    provenance = wd.provenance
    report = build_report(
        analysis_id, wd, inspection,
        base_manifest(config, provenance.env_id, provenance.project_dir),
        current_manifest(config, provenance.env_id, provenance.project_dir),
    )
    wd.save_analysis(workdir.AnalysisRecord(analysis_id, inspection.files.manifest, inspection.host_hash))
    return report


def changed(config: UserConfig, workdir_id: str, analysis_id: str) -> jdata.DictJsonData:
    """`scoter pull changed`: the paths added, removed or changed since the analysis.

    Nothing is analyzed: the working directory is snapshotted, and compared
    with the snapshot the analysis named.  `host-file` says whether the host
    file changed.
    """
    wd = workdir.open_workdir(config, workdir_id)
    record = wd.analysis(analysis_id)
    _, file_name = seed.files_location(config, seed.require_found(config))
    now, host_hash = wd.snapshot(lookup.limits(config), file_name)
    before, after = record.files.files, now.manifest.files
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": [p for p in seed.differences(record.files, now.manifest) if p in before and p in after],
        "host-file": host_hash != record.host_file,
    }
