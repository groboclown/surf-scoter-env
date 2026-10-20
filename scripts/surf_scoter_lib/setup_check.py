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
    home: Path | None = None,
) -> list[SetupProblem]:
    """Find setup `programs` that a project could supply.

    `path_env` is the `PATH` the programs are found on (by default, this
    process's), and `home` the home directory (by default, the user's).
    """
    if path_env is None:
        path_env = os.environ.get("PATH", "")
    if home is None:
        home = Path.home()
    problems: list[SetupProblem] = []
    for file, section, key, value in config.setup_programs():
        reason = _written_problem(value)
        if reason:
            problems.append(SetupProblem(file, f"{section}.programs.{key}", value, reason))

    # As found: anything inside the project, however it was referred to.
    roots = _project_roots(config, home)
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


def _project_roots(config: UserConfig, home: Path) -> list[tuple[str, Path]]:
    """The project's locations, and the repository holding it."""
    roots: list[tuple[str, Path]] = []
    project_dir = config.active_location("project-dir")
    project_config = config.active_location("project-config")
    workspace = config.active_location("dev-container-workspace-dir")
    if project_dir:
        roots.append(("the project directory", project_dir))
    if project_config:
        roots.append(("the project files directory", project_config.parent))
        if not project_dir:
            project_dir = project_config.parent.parent
    if workspace:
        roots.append(("the workspace", workspace))
    if project_dir:
        repository = repository_root(project_dir, home)
        if repository:
            roots.append(("the repository", repository))
    return roots


def repository_root(project_dir: Path, home: Path) -> Path | None:
    """The repository holding `project_dir`, as the configuration loader finds it.

    The nearest directory, at or above it, with a `.git` (a directory, or a
    file in a worktree or submodule) or `.hg`; the home directory and those
    above it don't count.
    """
    real_home = Path(os.path.realpath(home))
    start = Path(os.path.realpath(project_dir))
    for directory in [start, *start.parents]:
        if real_home.is_relative_to(directory):
            return None
        if (directory / ".git").exists() or (directory / ".hg").is_dir():
            return directory
    return None


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
