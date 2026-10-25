#SPDX:MIT
"""Checks on the system setup files that the configuration loader doesn't make.

A setup file's `programs` run on the host with the user's privileges.  One
that a project can supply or choose, such as `./venv/bin/python` (relative
to the project directory), runs the project's code there.  The loader
refuses setup files in a project; these checks find setup values that lead
into one.
"""
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from . import checks, jdata
from .user_config import UserConfig

# The tool's locations that depend on the project, or on where the tool runs.
PROJECT_LOCATIONS = (
    "project-dir",
    "vcs-root",
    "project-config",
    "cwd",
    "dev-container-path",
    "dev-container-workspace-dir",
    "dev-container-project-dir",
    "dev-container-settings-dir",
)


class SetupProblem(NamedTuple):
    """A setup value that could run a project's code on the host."""

    file: str | None
    """The setup file it's set in, if it's one setting."""

    key: str
    """Where: `section.programs.key`, or `PATH`."""

    value: str
    """The value as written, or as found."""

    reason: str

    def __str__(self) -> str:
        where = f"{self.file}: " if self.file else ""
        return f"{where}{self.key} ({self.value!r}): {self.reason}"


def check_programs(
    config: UserConfig,
    path_env: str | None = None,
) -> list[SetupProblem]:
    """Find setup `programs` that a project could supply.

    `path_env` is the `PATH` the programs are found on (by default, this
    process's).
    """
    if path_env is None:
        path_env = os.environ.get("PATH", "")
    problems: list[SetupProblem] = []
    for file, section, key, value in config.setup_programs():
        reason = _written_problem(value)
        if reason:
            problems.append(SetupProblem(file, f"{section}.programs.{key}", value, reason))

    # As found: anything inside the project, however it was referred to.
    roots = _project_roots(config)
    seen: set[tuple[str, str]] = set()
    for _, section, key, _ in config.setup_programs():
        if (section, key) in seen:
            continue
        seen.add((section, key))
        found = config.resolved_program(section, key)
        if not found:
            continue
        name = _inside(Path(found), roots)
        if name:
            problems.append(SetupProblem(
                None,
                f"{section}.programs.{key}",
                found,
                f"is inside the project ({name}); it would run the project's code on your computer",
            ))

    for entry in _relative_path_entries(path_env):
        problems.append(SetupProblem(
            None,
            "PATH",
            entry,
            "is relative, so programs given by name can be found in whatever directory "
            "the tool runs in, which is usually the project",
        ))
    return problems


def program_findings(config: UserConfig, path_env: str | None = None) -> list[checks.Finding]:
    """Everything wrong with the setup's programs, as the checks library's findings.

    `check_programs`' problems refuse, but a relative `PATH` entry, which
    the tools skip, only warns.  Then, for each program as found: whether
    another user could change it, and the setup's
    `host-commands` checks that match by where a program is (`paths`: version
    manager shims, which pick the real program from a file in the directory
    they run in).  `scoter user-setup` reports them; `scoter run` refuses to
    start on a refusal.
    """
    findings = [
        checks.Finding(
            checks.Severity.WARN if problem.key == "PATH" else checks.Severity.REFUSE,
            problem.key,
            (f"{problem.file}: " if problem.file else "") + f"{problem.value!r} {problem.reason}",
        )
        for problem in check_programs(config, path_env)
    ]
    seen: set[str] = set()
    for _, section, key, _ in config.setup_programs():
        where = f"{section}.programs.{key}"
        found = config.resolved_program(section, key)
        if where in seen or not found:
            continue
        seen.add(where)
        findings.extend(checks.writable_by_others(where, key, Path(found)))
        findings.extend(checks.path_findings(config, where, found))
    return findings


def _written_problem(value: str) -> str | None:
    """Why a `programs` value, as written, could lead into a project."""
    if value == "." or value.startswith(("./", ".\\")):
        return (
            "starts with './', so it's relative to the project directory; "
            "it would run the project's code on your computer"
        )
    for location in PROJECT_LOCATIONS:
        if f"${{scoter:{location}}}" in value:
            return (
                f"refers to `${{scoter:{location}}}`, which depends on the project or "
                f"where the tool runs; it could run the project's code on your computer"
            )
    is_relative = not (
        os.path.isabs(value) or value.startswith(("~", "${"))
    )
    if is_relative and ("/" in value or "\\" in value):
        return (
            "is a relative path, so it's found from whatever directory the tool runs in, "
            "which is usually the project; use an absolute path, or a bare name found on the PATH"
        )
    return None


def _project_roots(config: UserConfig) -> list[tuple[str, Path]]:
    """The project's locations, and the repository holding it (`vcs-root`)."""
    roots: list[tuple[str, Path]] = []
    project_dir = config.active_location("project-dir")
    project_config = config.active_location("project-config")
    workspace = config.active_location("dev-container-workspace-dir")
    vcs_root = config.active_location("vcs-root")
    if project_dir:
        roots.append(("the project directory", project_dir))
    if project_config:
        roots.append(("the project files directory", project_config.parent))
    if workspace:
        roots.append(("the workspace", workspace))
    if vcs_root:
        roots.append(("the repository", vcs_root))
    return roots


def _inside(path: Path, roots: Sequence[tuple[str, Path]]) -> str | None:
    """The name of the root `path` is in, once symlinks are resolved."""
    real = Path(os.path.realpath(path))
    for name, root in roots:
        if real.is_relative_to(os.path.realpath(root)):
            return name
    return None


def _relative_path_entries(path_env: str) -> list[str]:
    """The PATH entries that are relative; an empty one is the current directory."""
    return [
        entry if entry else "(empty: the current directory)"
        for entry in path_env.split(os.pathsep)
        if not entry or not os.path.isabs(entry)
    ]


def host_command_report(config: UserConfig) -> list[str]:
    """What the user should know about their setup's host command checks.

    The generator's notices (checks removed or replaced, overrides that do
    nothing), and each override in effect: what a check gives up, and for what.
    """
    report = list(config.host_command_notices())
    for check_id, check in sorted(config.host_command_checks().items()):
        overrides = check.get("overrides") or []
        commands = check.get("commands") or {}
        if not isinstance(overrides, list) or not isinstance(commands, dict):
            continue
        for target in overrides:
            names = ", ".join(f"`{name}`" for name in sorted(commands))
            report.append(
                f"`{check_id}` overrides `{target}` for {names}, "
                f"with `{check.get('action')}`"
            )
    return report


# ---------------------------------------------------------------------
# Guarded settings

# The `verification` value lists naming them: `guarded`, where any change is
# reported, and `guarded-removals`, deny-lists where adding entries only
# strengthens, so only a default entry left out is.
GUARDED = "guarded"
GUARDED_REMOVALS = "guarded-removals"
_ANY_KEY = "*"
_CHECK_KINDS = ("host-commands", "project-files", "egress-hosts")


@dataclass(frozen=True)
class GuardedChange:
    """A guarded setting a user setup file changes from the distribution's."""

    where: str
    """`section: kind: key`."""
    file: str
    """The user setup file that changes it."""
    yours: jdata.JsonData
    default: jdata.JsonData
    """The distribution's; None when it doesn't set it."""
    left_out: tuple[str, ...] = ()
    """For a `guarded-removals` list, the default's entries it leaves out."""
    removed: bool = False
    still_applies: bool = False
    """The entries left out still apply (the guard lists themselves)."""

    def __str__(self) -> str:
        if self.still_applies:
            return (
                f"{self.file} tries to take {len(self.left_out)} guard(s) out of `{self.where}`; "
                "they still apply, as the distribution's are always used"
            )
        if self.removed:
            return f"{self.file} removes `{self.where}`; the default is {_shown(self.default)}"
        if self.left_out:
            return (
                f"{self.file} leaves out of `{self.where}` the default's "
                + ", ".join(f"`{entry}`" for entry in self.left_out)
            )
        default = "not set" if self.default is None else _shown(self.default)
        return f"{self.file} sets `{self.where}` to {_shown(self.yours)}; the default is {default}"


def _shown(value: jdata.JsonData) -> str:
    return json.dumps(value, ensure_ascii=True)


def _files(config: UserConfig, source: str) -> list[jdata.DictJsonData]:
    files = config.raw("setup", "files")
    return [f for f in files if isinstance(f, dict) and f.get("source") == source] if isinstance(files, list) else []


def _set_in(files: list[jdata.DictJsonData], section: str, kind: str) -> dict[str, tuple[str, jdata.JsonData]]:
    """Each key of `section: kind` the files set: (the file, its value), the highest priority file's."""
    found: dict[str, tuple[str, jdata.JsonData]] = {}
    for file in files:
        groups = jdata.find_dict(file, "sections", section) or {}
        entries = groups.get(kind)
        if isinstance(entries, dict):
            for key, value in entries.items():
                found.setdefault(key, (str(file.get("file")), value))
    return found


def _guards(config: UserConfig, name: str) -> list[tuple[str, str, str]]:
    """The guarded `section:kind:key` names: the distribution's, and the user's additions.

    A user's setup can add to the lists, but not take an entry out of the
    distribution's: following instructions to remove a guard doesn't hide
    what it guards.
    """
    names: list[str] = []
    for source in ("distribution", "user"):
        found = _set_in(_files(config, source), "verification", "value-lists").get(name)
        if found and isinstance(found[1], list):
            names.extend(str(entry) for entry in found[1] if str(entry) not in names)
    guards = []
    for entry in names:
        parts = entry.split(":")
        if len(parts) == 3 and all(parts):
            guards.append((parts[0], parts[1], parts[2]))
    return guards


def guarded_changes(config: UserConfig) -> list[GuardedChange]:
    """The guarded settings the user's setup files change from the distribution's.

    Only setup files of the user's are compared with the distribution's
    (each as written, in the universal configuration file's `setup`).  A key
    the user's file doesn't set isn't reported, nor one set to the default.
    """
    user_files = _files(config, "user")
    if not user_files:
        return []
    dist_files = _files(config, "distribution")
    changes: list[GuardedChange] = []
    for name, removals_only in ((GUARDED, False), (GUARDED_REMOVALS, True)):
        for section, kind, key in _guards(config, name):
            yours = _set_in(user_files, section, kind)
            defaults = _set_in(dist_files, section, kind)
            # Every key the distribution sets: one of the user's own is an
            # addition (an override among them is reported with the others).
            keys = sorted(defaults) if key == _ANY_KEY else [key]
            for k in keys:
                if k not in yours:
                    continue
                file, value = yours[k]
                default = defaults.get(k, (None, None))[1]
                if value == default:
                    continue
                where = f"{section}: {kind}: {k}"
                if value is None:
                    changes.append(GuardedChange(where, file, None, default, removed=True))
                elif removals_only and isinstance(value, list) and isinstance(default, list):
                    left_out = tuple(str(e) for e in default if e not in value)
                    if left_out:
                        changes.append(GuardedChange(
                            where, file, value, default, left_out,
                            still_applies=section == "verification" and k in (GUARDED, GUARDED_REMOVALS),
                        ))
                elif not (removals_only and default is None):
                    changes.append(GuardedChange(where, file, value, default))
    return changes


def simple_check_overrides(config: UserConfig) -> list[str]:
    """Each `project-files` and `egress-hosts` override in effect: what it relaxes, for what."""
    report: list[str] = []
    for group, field_name in (("project-files", "patterns"), ("egress-hosts", "hosts")):
        for check_id, check in sorted(config.simple_checks(group).items()):
            overrides = jdata.find_str_list(check, "overrides")
            if overrides:
                matched = ", ".join(f"`{m}`" for m in jdata.find_str_list(check, field_name))
                report.append(
                    f"`{check_id}` overrides " + ", ".join(f"`{o}`" for o in overrides)
                    + f" for {matched}, with `{jdata.find_str(check, 'action')}`"
                )
    return report


def setup_report(config: UserConfig) -> list[str]:
    """Everything the user should know about how their setup changes the defaults' protections:
    the host command checks (`host_command_report`), the other checks' overrides, and the
    guarded settings it changes."""
    return [
        *host_command_report(config),
        *simple_check_overrides(config),
        *(str(change) for change in guarded_changes(config)),
    ]
