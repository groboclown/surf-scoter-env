#SPDX:MIT
"""The checks on a project's settings, for the review and the run.

Everything here works from the universal configuration file (and the
project's files on disk), using the setup's lists: the `host-commands`
checks (combined by the generator into `host-command-checks`), and the
`scoter: value-lists` for secret-looking files and hosts that can carry
data out.  Nothing is hard-coded but the rules for reading them.

Each finding has a severity (`refuse`, `warn`, or `note` for what the
review should show without judging it), where it is in the project file,
and an explanation for the review's generated comments.  See step 11 of
docs/contributors/projects-v1.md.
"""
import fnmatch
import os
import re
import stat
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from . import gateway, jdata
from .err import ConfigErr
from .user_config import UserConfig
from .verified import snapshot

SECRET_FILES = "secret-file-patterns"  # nosec: B105  # A setting's name, not a secret.
DATA_OUT_HOSTS = "egress-data-out-hosts"


class Severity(StrEnum):
    """How much a finding matters."""

    REFUSE = "refuse"
    WARN = "warn"
    NOTE = "note"


_ACTIONS = {"refuse": Severity.REFUSE, "warn": Severity.WARN, "allow": Severity.NOTE}
_ORDER = {"allow": 0, "warn": 1, "refuse": 2}


@dataclass(frozen=True)
class Finding:
    """One thing the checks found."""

    severity: Severity
    # Where, in the project file's terms: `commands[0].program`, `egress[1]`,
    # or a file, such as `dev-env/Dockerfile`.
    key: str
    message: str
    # The setup's `host-commands` checks (`section:key`) it comes from.
    checks: tuple[str, ...] = ()

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data."""
        return {
            "severity": str(self.severity),
            "key": self.key,
            "message": self.message,
            "checks": list(self.checks),
        }


@dataclass(frozen=True)
class Resolved:
    """A project command's program, as found on this host."""

    index: int
    command: str
    # The program's name: the command's `program`, or else its `command`.
    name: str
    # The absolute executable, or None when it isn't on this host.
    executable: str | None
    # `command-map` (the setup maps the name) or `path`.
    source: str


@dataclass
class Report:
    """What the checks found, and how the project's programs resolved."""

    resolved: list[Resolved] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def refused(self) -> list[Finding]:
        """The findings that stop the project from being accepted."""
        return [f for f in self.findings if f.severity == Severity.REFUSE]

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data."""
        return {
            "resolved": [
                {
                    "index": r.index, "command": r.command, "name": r.name,
                    "executable": r.executable, "source": r.source,
                }
                for r in self.resolved
            ],
            "findings": [f.to_json() for f in self.findings],
        }


# ---------------------------------------------------------------------
# Reading the setup's checks and the project's commands
#
# The universal configuration file comes through `UserConfig`.  Its parts
# are read with jdata's typed lookups: a check from the generator always
# has all its fields; a project command has only the ones it was written with.


def _texts(data: jdata.JsonData, *path: jdata.JsonPathPart) -> list[str]:
    """The text list at the path, which must be there (a check's fields always are)."""
    return [jdata.req_str(data, *key) for key in jdata.opt_list_keys(data, *path)]


def _check_commands(check: jdata.DictJsonData) -> dict[str, str | None]:
    """A check's `commands`: each name, and the program it runs (None when it isn't there)."""
    ret: dict[str, str | None] = {}
    for key in jdata.opt_dict_keys(check, "commands"):
        ret[str(key[-1])] = jdata.find_str(check, *key)
    return ret


def _locations(config: UserConfig) -> dict[str, Path]:
    """The project's places, by name, with symlinks resolved."""
    ret: dict[str, Path] = {}
    for name in ("project-dir", "vcs-root", "dev-container-workspace-dir"):
        location = config.active_location(name)
        if location is not None:
            ret[f"scoter:{name}"] = Path(os.path.realpath(location))
    return ret


# ---------------------------------------------------------------------
# Programs


def find_on_path(name: str, path_env: str) -> str | None:
    """`name` on the `PATH`'s absolute directories, as the generator finds programs."""
    for directory in path_env.split(os.pathsep):
        if not directory or not os.path.isabs(directory):
            continue
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def resolve_programs(config: UserConfig, path_env: str) -> list[Resolved]:
    """Each project command's program: through the setup's `command-map`, or else the `PATH`."""
    mapped = config.host_command_map()
    ret: list[Resolved] = []
    for i, command in enumerate(config.project_commands()):
        cmd = jdata.req_str(command, "command")
        name = jdata.find_str(command, "program") or cmd
        if name in mapped:
            ret.append(Resolved(i, cmd, name, mapped[name], "command-map"))
        else:
            ret.append(Resolved(i, cmd, name, find_on_path(name, path_env), "path"))
    return ret


def _real(path: str | None) -> str | None:
    return os.path.realpath(path) if path else None


def _base_name(path: str) -> str:
    name = os.path.basename(path)
    return os.path.splitext(name)[0] if sys.platform == "win32" else name


def _matches_name(check: jdata.DictJsonData, name: str, real: str | None) -> bool:
    """Whether the check's names match the program `name`, whose executable is `real`."""
    commands = _check_commands(check)
    if name in commands:
        return True
    if real and any(p is not None and os.path.realpath(p) == real for p in commands.values()):
        return True
    base = _base_name(real) if real else None
    return any(
        fnmatch.fnmatchcase(name, p) or (base is not None and fnmatch.fnmatchcase(base, p))
        for p in _texts(check, "name-patterns")
    )


def _has_names(check: jdata.DictJsonData) -> bool:
    return bool(_check_commands(check)) or bool(_texts(check, "name-patterns"))


def _code_options(command: jdata.DictJsonData, options: Sequence[str]) -> list[tuple[int, str]]:
    """The args where one of `options` takes a value the container fills in: (arg index, option)."""
    args = jdata.req_list(command, "args")
    found: list[tuple[int, str]] = []
    for j, token in enumerate(args):
        following = args[j + 1] if j + 1 < len(args) else None
        if isinstance(token, str) and token in options and isinstance(following, dict) \
                and "any" in following:
            found.append((j + 1, token))
        prefix = jdata.find_str(token, "any", "prefix") if isinstance(token, dict) else None
        if prefix is not None:
            for option in options:
                if prefix == option or prefix.startswith(option + "="):
                    found.append((j, option))
    return found


# Each `env` `allow` entry a command gets, as (key, entry): given the command and its key.
EnvAllow = Callable[[jdata.DictJsonData, str], list[tuple[str, str]]]


def _project_env_allow(config: UserConfig) -> EnvAllow:
    """A project file command's `allow` entries: the project's, then its own."""
    common = [("env.allow", e) for e in config.project_env_allow()]

    def allow(command: jdata.DictJsonData, at: str) -> list[tuple[str, str]]:
        return [*common, *((f"{at}.env.allow", e) for e in jdata.find_str_list(command, "env", "allow"))]
    return allow


def _plan_env_allow(command: jdata.DictJsonData, at: str) -> list[tuple[str, str]]:
    """An accepted command's `allow` entries: its whole plan's."""
    return [(f"{at}.env.allow", e) for e in jdata.find_str_list(command, "env", "allow")]


@dataclass
class _Hit:
    check_id: str
    check: jdata.DictJsonData
    key: str
    # What matched besides the name: an option or an `env` entry.
    item: str | None = None


def check_host_commands(
    config: UserConfig,
    resolved: Sequence[Resolved],
    commands: Sequence[jdata.DictJsonData] | None = None,
    env_allow: EnvAllow | None = None,
) -> list[Finding]:
    """The setup's `host-commands` checks against the commands (by default, the project file's).

    For each command, the checks without `overrides` that match it count,
    the most restrictive action winning; then each override for the command
    takes the place of the check it overrides (step 4).
    """
    checks = config.host_command_checks()
    if commands is None:
        commands = config.project_commands()
    if env_allow is None:
        env_allow = _project_env_allow(config)
    findings: list[Finding] = []
    for r in resolved:
        command = commands[r.index]
        at = f"commands[{r.index}]"
        if r.executable is None:
            findings.append(Finding(
                Severity.REFUSE, f"{at}.program",
                f"`{r.name}` isn't on this computer"
                + (" (your setup's `command-map` names a program that isn't there)"
                   if r.source == "command-map" else ""),
            ))
            continue
        real = _real(r.executable)
        hits = [
            h for h in _hits(env_allow, checks, command, at, r.name, real)
            if not _texts(h.check, "overrides")
        ]
        if not hits:
            findings.append(Finding(
                Severity.NOTE, f"{at}.program",
                f"No check in your setup covers `{r.name}`; it runs {r.executable}.",
            ))
            continue
        overridden: list[tuple[_Hit, str]] = []
        for override_id, override in checks.items():
            targets = _texts(override, "overrides")
            if not targets or r.name not in _check_commands(override):
                continue
            limited = set(_texts(override, "options")) | set(_texts(override, "env"))
            for hit in list(hits):
                if hit.check_id in targets and (not limited or hit.item in limited):
                    hits.remove(hit)
                    overridden.append((hit, override_id))
                    hits.append(_Hit(override_id, override, hit.key, hit.item))
        for hit in hits:
            findings.append(Finding(
                _ACTIONS.get(jdata.req_str(hit.check, "action"), Severity.REFUSE),
                hit.key,
                jdata.req_str(hit.check, "reason"),
                (hit.check_id,),
            ))
        for hit, by in overridden:
            findings.append(Finding(
                Severity.NOTE, hit.key,
                f"`{hit.check_id}` would apply here, but your setup's `{by}` overrides it.",
                (hit.check_id, by),
            ))
    return findings


def _hits(
    env_allow: EnvAllow,
    checks: dict[str, jdata.DictJsonData],
    command: jdata.DictJsonData,
    at: str,
    name: str,
    real: str | None,
) -> Iterable[_Hit]:
    for check_id, check in checks.items():
        options = _texts(check, "options")
        env = _texts(check, "env")
        paths = _texts(check, "paths")
        if paths:
            if real and any(
                real == os.path.realpath(p) or real.startswith(os.path.realpath(p) + os.sep)
                for p in paths
            ):
                yield _Hit(check_id, check, f"{at}.program")
            continue
        if _has_names(check) and not _matches_name(check, name, real):
            continue
        if options:
            for j, option in _code_options(command, options):
                yield _Hit(check_id, check, f"{at}.args[{j}]", option)
        elif env:
            for key, entry in env_allow(command, at):
                if entry in env:
                    yield _Hit(check_id, check, key, entry)
        else:
            yield _Hit(check_id, check, f"{at}.program")


def check_program_paths(
    config: UserConfig, resolved: Sequence[Resolved], path_env: str,
) -> list[Finding]:
    """Where the programs are: not in the project, and not where others can change them."""
    findings: list[Finding] = []
    relative = [d for d in path_env.split(os.pathsep) if not d or not os.path.isabs(d)]
    if relative:
        findings.append(Finding(
            Severity.WARN, "PATH",
            "Your PATH has relative or empty entries ("
            + ", ".join(repr(d) for d in relative)
            + "); the tools skip them, as they'd find programs in whatever directory they run in.",
        ))
    locations = _locations(config)
    for r in resolved:
        if r.executable is None:
            continue
        key = f"commands[{r.index}].program"
        real = Path(os.path.realpath(r.executable))
        for name, location in locations.items():
            if real.is_relative_to(location) or Path(r.executable).is_relative_to(location):
                findings.append(Finding(
                    Severity.REFUSE, key,
                    f"`{r.name}` runs {r.executable}, which is in the project (`{name}`); "
                    "a host command never runs a program from the project.",
                ))
                break
        findings.extend(_writable_by_others(key, r.name, Path(r.executable)))
    return findings


def _writable_by_others(key: str, name: str, path: Path) -> list[Finding]:
    """Whether another user could change the program, or replace it."""
    if sys.platform == "win32":
        return []
    found: list[Finding] = []
    uid = os.getuid()
    seen: set[Path] = set()
    for start in (path, Path(os.path.realpath(path))):
        for current in (start, *start.parents):
            if current in seen:
                continue
            seen.add(current)
            try:
                st = os.lstat(current)
            except OSError:
                continue
            mode = st.st_mode
            # A symlink's own mode means nothing: its directory, checked as
            # well, says who can change it, and its target is checked too.
            if stat.S_ISLNK(mode):
                continue
            is_dir = stat.S_ISDIR(mode)
            what = "directory" if is_dir else "file"
            if st.st_uid not in (0, uid):
                found.append(Finding(
                    Severity.REFUSE, key,
                    f"`{name}`'s {what} {current} belongs to another user, who could change "
                    "what runs.",
                ))
            elif is_dir and mode & stat.S_ISVTX:
                # Sticky (like /tmp): others can add files, but not replace yours.
                continue
            elif mode & stat.S_IWOTH:
                found.append(Finding(
                    Severity.REFUSE, key,
                    f"Anyone can write to `{name}`'s {what} {current}, and change what runs.",
                ))
            elif mode & stat.S_IWGRP:
                found.append(Finding(
                    Severity.WARN, key,
                    f"Other members of its group can write to `{name}`'s {what} {current}.",
                ))
    return found


# ---------------------------------------------------------------------
# Egress


def _host_covers(rule_host: str, pattern: str) -> bool:
    """Whether a rule for `rule_host` allows a host `pattern` names."""
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return rule_host == pattern or rule_host.endswith("." + suffix)
    if rule_host == pattern:
        return True
    return rule_host.startswith("*.") and pattern.endswith("." + rule_host[2:])


def check_egress(
    config: UserConfig, entries: list[jdata.DictJsonData] | None = None,
) -> list[Finding]:
    """Plain `http`, and hosts that can carry data out whatever the other rules say.

    The rules are `entries` when given (a review's), or else the project file's.
    """
    try:
        rules = gateway.rules_of(entries if entries is not None else config.project_egress())
    except gateway.RuleErr as err:
        return [Finding(Severity.REFUSE, "egress", str(err))]
    data_out = config.value_list("scoter", DATA_OUT_HOSTS)
    findings: list[Finding] = []
    for i, rule in enumerate(rules):
        key = f"egress[{i}]"
        if rule.protocol == "http":
            findings.append(Finding(
                Severity.WARN, key,
                f"`{rule.host}` over plain HTTP can be read, and its downloads changed, by "
                "anyone in between.",
            ))
        covered = [p for p in data_out if _host_covers(rule.host, p)]
        if covered:
            findings.append(Finding(
                Severity.WARN, key,
                f"`{rule.host}` lets the container send data out ("
                + ", ".join(f"`{p}`" for p in covered)
                + " is a place anyone can upload to), whatever the other rules say.",
            ))
    return findings


# ---------------------------------------------------------------------
# The project's files


_FROM = re.compile(r"FROM\s+(.*)", re.IGNORECASE)
_STAGE = re.compile(r"\s+AS\s+(\S+)\s*$", re.IGNORECASE)


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    current = ""
    for line in snapshot.canonical_dockerfile(text).splitlines():
        if line.startswith("# "):
            continue
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        lines.append(current + line)
        current = ""
    if current:
        lines.append(current)
    return lines


def from_images(text: str, local_tags: Iterable[str]) -> list[str]:
    """The images a Dockerfile builds from: each `FROM`, but `scratch`, a local
    parent's tag, or an earlier stage, in order, each once."""
    local = set(local_tags) | {"scratch"}
    stages: set[str] = set()
    images: list[str] = []
    for line in _logical_lines(text):
        match = _FROM.match(line.strip())
        if not match:
            continue
        rest = match.group(1)
        stage = _STAGE.search(rest)
        if stage:
            rest = rest[: stage.start()]
        words = [w for w in rest.split() if not w.startswith("--")]
        image = words[0] if words else ""
        if image and image not in local and image.lower() not in stages and image not in images:
            images.append(image)
        if stage:
            stages.add(stage.group(1).lower())
    return images


def from_findings(rel: str, text: str, local_tags: Iterable[str]) -> list[Finding]:
    """`FROM` lines without a digest: a tag can point at new content any time."""
    findings: list[Finding] = []
    for image in from_images(text, local_tags):
        if "@sha256:" in image:
            continue
        if "$" in image:
            message = f"`FROM {image}` comes from a build argument, so what it builds on can't be told."
        else:
            message = (
                f"`FROM {image}` has no digest (`@sha256:...`): the tag can point at new content "
                "at any time, so what you accept isn't what may be built."
            )
        findings.append(Finding(Severity.WARN, rel, message))
    return findings


def _secret_like(rel: str, patterns: Sequence[str], is_dir: bool) -> str | None:
    name = rel.rsplit("/", 1)[-1]
    for pattern in patterns:
        if pattern.endswith("/"):
            if is_dir and fnmatch.fnmatchcase(name, pattern[:-1]):
                return pattern
        elif fnmatch.fnmatchcase(name, pattern):
            return pattern
    return None


def check_project_files(config: UserConfig) -> list[Finding]:
    """The project file and its container directories (those in the project files directory)."""
    project = config.active_location("project-dir")
    files_name = config.optional_value("scoter", "project-files-dir")
    file_name = config.optional_value("scoter", "project-file-name")
    if project is None or files_name is None or file_name is None:
        return []
    files_dir = Path(os.path.realpath(project / files_name))
    secrets = config.value_list("scoter", SECRET_FILES)
    builds = config.dev_container_parent_builds() if config.has_dev_container() else []
    local_tags = [tag for tag, _ in builds]
    dirs: list[Path | None] = [config.active_location("dev-container-path")]
    dirs.extend(directory for _, directory in builds)
    findings: list[Finding] = []
    try:
        st = os.lstat(files_dir / str(file_name))
        if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
            findings.append(Finding(
                Severity.WARN, str(file_name),
                "The project file has other hard links; it's reviewed whatever it's linked to.",
            ))
    except OSError:
        pass
    for directory in dirs:
        if directory is None:
            continue
        real = Path(os.path.realpath(directory))
        if not real.is_relative_to(files_dir) or real == files_dir:
            continue
        findings.extend(_container_findings(real, real.relative_to(files_dir).as_posix(), secrets, local_tags))
    return findings


def _container_findings(
    directory: Path, rel: str, secrets: Sequence[str], local_tags: Sequence[str],
) -> list[Finding]:
    findings: list[Finding] = []
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError as err:
        return [Finding(Severity.REFUSE, rel, f"can't be read: {err}")]
    for entry in entries:
        child = f"{rel}/{entry.name}"
        is_dir = entry.is_dir(follow_symlinks=False)
        pattern = _secret_like(child, secrets, is_dir)
        if pattern and not entry.is_symlink():
            findings.append(Finding(
                Severity.WARN, child,
                f"Looks like a secret (`{pattern}`): everything in a container directory goes "
                "into the image, where the container can read it.",
            ))
        if entry.is_symlink():
            findings.append(Finding(
                Severity.REFUSE, child,
                f"A symlink (to {os.readlink(entry.path)}); a container directory can't have any, "
                "as it could bring in files from outside it.",
            ))
        elif is_dir:
            findings.extend(_container_findings(Path(entry.path), child, secrets, local_tags))
        elif entry.is_file(follow_symlinks=False):
            if entry.stat(follow_symlinks=False).st_nlink > 1:
                findings.append(Finding(
                    Severity.REFUSE, child,
                    "Has other hard links; a container directory can't share files with anything "
                    "outside it.",
                ))
            elif snapshot.is_dockerfile(entry.name):
                try:
                    text = Path(entry.path).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as err:
                    findings.append(Finding(Severity.WARN, child, f"can't be read: {err}"))
                else:
                    findings.extend(from_findings(child, text, local_tags))
        else:
            findings.append(Finding(
                Severity.REFUSE, child,
                "A special file (a FIFO, socket or device); a container directory can't have any.",
            ))
    return findings


# ---------------------------------------------------------------------
# Everything


def check_project(config: UserConfig, path_env: str | None = None) -> Report:
    """Every check on the project in the universal configuration file.

    `path_env` is the `PATH` programs are found on (by default, this process's).
    A badly formed file is a `ConfigErr`.
    """
    if path_env is None:
        path_env = os.environ.get("PATH", "")
    try:
        resolved = resolve_programs(config, path_env)
        findings = check_host_commands(config, resolved)
        findings.extend(check_program_paths(config, resolved, path_env))
        findings.extend(check_egress(config))
        findings.extend(check_project_files(config))
    except (KeyError, TypeError, ValueError) as err:
        raise ConfigErr(f"bad universal configuration file: {err}") from err
    return Report(resolved, findings)


# ---------------------------------------------------------------------
# A review's settings


def check_review(config: UserConfig, path_env: str | None = None) -> Report:
    """Every check on the accepted settings a review file gives (the file's `review`).

    The same checks as the project's, on what the user left in the review:
    each command's program as given (an absolute path, nothing looked up),
    its whole environment plan, the egress rules, and the project's
    container directories when the review uses the project's container.
    Then the review's `$override`s (step 12): a refusal from the setup's
    checks that the override names, every one of them, is set aside, as a
    note; a refusal for safety (the program's missing, in the project, or
    writable by others; a container file that's a link) never is; and an
    override naming a check that doesn't refuse the item is refused itself.
    No review, or a rejected one, is an empty report.
    """
    if path_env is None:
        path_env = os.environ.get("PATH", "")
    accepted = config.review_accepted()
    if accepted is None:
        return Report()
    try:
        commands = [jdata.req_dict(accepted, *key) for key in jdata.find_list_keys(accepted, "commands")]
        resolved = []
        for i, command in enumerate(commands):
            program = jdata.req_str(command, "program")
            present = os.path.isfile(program) and os.access(program, os.X_OK)
            resolved.append(Resolved(
                i, jdata.req_str(command, "command"), _base_name(program),
                program if present else None, "review",
            ))
        findings = check_host_commands(config, resolved, commands, _plan_env_allow)
        findings.extend(check_program_paths(config, resolved, path_env))
        egress = [jdata.req_dict(accepted, *key) for key in jdata.find_list_keys(accepted, "egress")]
        findings.extend(check_egress(config, egress))
        if jdata.find(accepted, "dev-container", "use") in (None, "project"):
            findings.extend(check_project_files(config))
        for kind in ("commands", "egress"):
            overrides = {
                jdata.req_int(accepted, *key, "index"): jdata.find_str_list(accepted, *key, "checks")
                for key in jdata.find_list_keys(accepted, "overrides", kind)
            }
            findings = apply_overrides(findings, kind, overrides)
    except (KeyError, TypeError, ValueError) as err:
        raise ConfigErr(f"bad universal configuration file: review: {err}") from err
    return Report(resolved, findings)


def apply_overrides(
    findings: Sequence[Finding], kind: str, overrides: dict[int, list[str]],
) -> list[Finding]:
    """The findings once the user's `$override`s on `kind` (`commands` or `egress`) apply."""
    ret = list(findings)
    for index, names in sorted(overrides.items()):
        at = f"{kind}[{index}]"

        def on_item(finding: Finding, at: str = at) -> bool:
            return finding.key == at or finding.key.startswith((at + ".", at + "["))

        # Worked out before anything is added for this item.
        refusals = [f for f in ret if on_item(f) and f.severity == Severity.REFUSE]
        refusing = {check for f in refusals for check in f.checks}
        safety = [f for f in refusals if not f.checks]
        for name in names:
            if name not in refusing:
                ret.append(Finding(
                    Severity.REFUSE, f"{at}.$override",
                    f"`{name}` doesn't refuse this item; an override names only the setup "
                    "checks that refuse it.",
                ))
        if safety:
            ret.append(Finding(
                Severity.REFUSE, f"{at}.$override",
                "Can't be overridden: it's refused for a reason that isn't a check in your "
                "setup (" + safety[0].message.rstrip(".") + ").",
            ))
            continue
        ret = [
            Finding(
                Severity.NOTE, f.key,
                "You overrode " + ", ".join(f"`{c}`" for c in f.checks) + f" here: {f.message}",
                f.checks,
            )
            if on_item(f) and f.severity == Severity.REFUSE and f.checks
            and set(f.checks) <= set(names)
            else f
            for f in ret
        ]
    return ret

