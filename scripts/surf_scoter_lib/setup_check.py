#SPDX:MIT
"""Checks on the system setup files that the configuration loader doesn't make.

A setup file's `programs` run on the host with the user's privileges.  One
that a project can supply or choose, such as `./venv/bin/python` (relative
to the project directory), runs the project's code there.  The loader
refuses setup files in a project; these checks find setup values that lead
into one.
"""
import os
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from .user_config import UserConfig

# The tool's locations that depend on the project, or on where the tool runs.
PROJECT_LOCATIONS = (
    "project-dir",
    "vcs-root",
    "project-config",
    "cwd",
    "dev-container-path",
    "dev-container-workspace-dir",
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
