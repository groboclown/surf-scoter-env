#SPDX:MIT
"""What `scoter run` runs: the configuration in use, staged, and checked again.

The run finds the project (`scoter`'s found scope) but never parses its
files.  It runs the environment's configuration in use:

* **Locked:** restored from the store into a private staging directory,
  each blob checked against its hash, with its host file.
* **Unlocked:** the unlocked copy, read as seeding reads a source (links,
  special files and the limits refused) and staged; never built from where
  it is.  Its commands are resolved as the run starts, through the path
  checks, and the changes listed: BRIDGE-11 is knowingly open while unlocked.

The generator then reads only the staged copy (`--config-dir`) and its host
file (`--host-file`), so host commands run only from the host file's paths.
On every run:

* the checks run again, and a refusal stops the run, saying which check,
  from which setup file, and how to relax it (VER-22);
* the review timers (VER-20) and the unlock reminders (VER-33) are applied;
* files in the unlocked directory of a locked configuration are warned of
  (VER-36);
* the project's files are hashed, never parsed, and compared with the last
  pull's: a one-line notice when they differ (VER-21);
* configurations retired while an environment ran them are removed once
  nothing runs them (VER-40).

The run's SUC records the configuration it runs (`configuration`), so a
later retirement knows it's in use.
"""
import datetime
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import checks, fileutil, host_file, jdata, review
from .active_env import ActiveEnvironmentSet
from .err import SurfScoterErr
from .pull import seed, unlocked
from .user_config import UserConfig
from .verified import configs, lookup, snapshot, store

# The run's SUC key recording the configuration it runs.
CONFIGURATION = "configuration"
FILES_DIR = "files"


class RunRefused(SurfScoterErr):
    """The run can't start: there's no configuration, or the checks refuse it."""


@dataclass
class Prepared:
    """The run's SUC, from the staged configuration, and what to tell the developer."""

    config: UserConfig
    staged: Path
    # The locked configuration run; None when it's the unlocked copy.
    config_id: str | None
    notices: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def running_config_ids(config: UserConfig) -> frozenset[str]:
    """The configurations the running environments use, from their records' SUCs."""
    file = config.optional_path("scoter", "running-envs")
    if file is None or not file.exists():
        return frozenset()
    found: set[str] = set()
    for env in ActiveEnvironmentSet.read(file).environs.values():
        config_id = jdata.find_str(env.suc, CONFIGURATION, "id") if isinstance(env.suc, dict) else None
        if config_id:
            found.add(config_id)
    return frozenset(found)


def _days(value: int | None) -> int:
    return value if value and value > 0 else 0


def _listing(run: UserConfig) -> list[str]:
    """The egress rules and host commands the configuration allows, cleaned, one per line."""
    lines = [
        "  egress: " + review.clean(jdata.find_str(rule, "host") or "?")
        for rule in run.project_egress()
    ]
    programs = run.host_file_programs() or {}
    for command in run.project_commands():
        name = jdata.find_str(command, "program") or jdata.req_str(command, "command")
        entry = programs.get(name)
        where = jdata.find_str(entry, "path") if entry else None
        lines.append(f"  host command: {review.clean(jdata.req_str(command, 'command'))} runs {review.clean(where or name)}")
    return lines


def _refusal(run: UserConfig, findings: Sequence[checks.Finding]) -> str:
    """Why the checks refuse the run: each finding, its check, and where the check comes from."""
    known = run.host_command_checks()
    lines = ["The checks refuse this configuration, so it can't run:"]
    for finding in findings:
        line = f"  {review.clean(finding.key)}: {review.clean(finding.message)}"
        for check_id in finding.checks:
            files = jdata.find_str_list(known.get(check_id, {}), "files")
            line += f" (`{check_id}`" + (f", from {', '.join(files)}" if files else "") + ")"
        lines.append(line)
    lines.append(
        "Change the configuration (`scoter unlock`, then `scoter lock`), or relax the check in your "
        "setup file with a check that `overrides` it for this command; see docs/system-config-file.md."
    )
    return "\n".join(lines)


def _stage_locked(
    verified: store.VerifiedStore, link: configs.Link, dest: Path, limits: snapshot.SnapshotLimits,
) -> configs.LockedConfig:
    assert link.in_use is not None  # nosec: B101  # the caller checked
    in_use = configs.load_config(verified, link.id, link.in_use)
    if in_use is None:
        raise RunRefused(
            f"Your configuration in use ({link.in_use}) isn't in the store any more: run `scoter pull`, "
            "or `scoter switch` to another."
        )
    stage_config(verified, in_use, dest, limits)
    return in_use


def stage_config(
    verified: store.VerifiedStore, config: configs.LockedConfig, dest: Path, limits: snapshot.SnapshotLimits,
) -> None:
    """Restore a locked configuration into `dest` (empty, private): `files/`, and its host file beside it."""
    files = dest / FILES_DIR
    files.mkdir(mode=fileutil.MODE_USER_RWX)
    snapshot.restore(verified, config.snapshot, files, limits)
    host_file.write(dest, config.host_file)


def check_config(
    found_config: UserConfig,
    verified: store.VerifiedStore,
    config: configs.LockedConfig,
    dest: Path,
    *,
    generate: seed.Generate,
    path_env: str | None = None,
) -> UserConfig:
    """Stage a locked configuration into `dest`, and run the checks a run would (`scoter switch`).

    A refusal is a `RunRefused`, as it would stop a run.
    """
    found = seed.require_found(found_config)
    stage_config(verified, config, dest, lookup.limits(found_config))
    run = _generate(
        found_config,
        [*found.generator_args(), "--config-dir", str(dest / FILES_DIR), "--host-file", str(dest / host_file.FILE_NAME)],
        dest, "generator", generate,
    )
    report = checks.check_with_host_file(run, path_env)
    if report.refused:
        raise RunRefused(_refusal(run, report.refused))
    return run


def _stage_unlocked(root: Path, file_name: str, dest: Path, limits: snapshot.SnapshotLimits) -> jdata.DictJsonData:
    try:
        source, host = unlocked.read(root, file_name, limits)
    except unlocked.UnlockedErr as err:
        raise RunRefused(str(err)) from err
    files = dest / FILES_DIR
    files.mkdir(mode=fileutil.MODE_USER_RWX)
    snapshot.write_files(source.manifest, files, limits, lambda rel, _: source.contents[rel])
    return host if host is not None else host_file.empty()


def _generate(
    found_config: UserConfig, args: list[str], dest: Path, name: str, generate: seed.Generate,
) -> UserConfig:
    work = dest / name
    work.mkdir(mode=fileutil.MODE_USER_RWX)
    data, problems = generate(found_config, args, work)
    if data is None:
        raise RunRefused("Your configuration can't be read: " + "; ".join(problems))
    return UserConfig(data)


def prepare(
    found_config: UserConfig,
    dest: Path,
    *,
    generate: seed.Generate,
    now: datetime.datetime | None = None,
    path_env: str | None = None,
) -> Prepared:
    """Stage the configuration in use into `dest` (empty, private), and make the run's SUC from it."""
    now = now or store.now_utc()
    found = seed.require_found(found_config)
    files_dir, file_name = seed.files_location(found_config, found)
    limits = lookup.limits(found_config)
    root = found_config.optional_path("verification", "store")
    project_dir = os.path.realpath(found.project_dir)
    env_id = store.project_id(found_config, project_dir)
    verified = store.VerifiedStore.open(root) if root is not None and root.is_dir() else None
    link = None if verified is None else configs.load_link(verified, project_dir, env_id)
    if verified is None or link is None or (link.in_use is None and link.unlocked is None):
        raise RunRefused(
            "You have no configuration for this project: run `scoter pull` to review the project's "
            "files and make one."
        )
    with verified.locked():
        # Retired while an environment ran them, and no longer (VER-40).
        if configs.remove_retired(verified, link, running_config_ids(found_config)):
            configs.save_link(verified, link)

    notices: list[str] = []
    warnings: list[str] = []
    base_args = [*found.generator_args(), "--config-dir", str(dest / FILES_DIR)]
    in_use: configs.LockedConfig | None = None
    if link.unlocked is not None:
        try:
            root = unlocked.root(found_config, env_id)
        except unlocked.UnlockedErr as err:
            raise RunRefused(str(err)) from err
        previous = _stage_unlocked(root, file_name, dest, limits)
        staged = _generate(found_config, base_args, dest, "generator-commands", generate)
        resolution = host_file.resolve(staged, path_env, previous)
        if resolution.report.refused:
            raise RunRefused(_refusal(staged, resolution.report.refused))
        warnings.extend(f"Host commands, resolved now: {review.clean(c)}" for c in resolution.changes)
        host_file.write(dest, resolution.contents)
    else:
        in_use = _stage_locked(verified, link, dest, limits)
        base = found_config.optional_path("pull", "unlocked-dir")
        stale = None if base is None else base / env_id
        if stale is not None and unlocked.holds_files(stale):
            warnings.append(
                f"{stale} holds files, but your configuration is locked: they aren't used.  "
                "`scoter unlock` refuses while they're there; move or remove them."
            )
    run = _generate(
        found_config, [*base_args, "--host-file", str(dest / host_file.FILE_NAME)], dest, "generator", generate,
    )

    # The checks, on every run (VER-22).
    report = checks.check_with_host_file(run, path_env)
    if report.refused:
        raise RunRefused(_refusal(run, report.refused))

    if link.unlocked is not None:
        days = (now - link.unlocked.at).days
        limit = _days(found_config.optional_int_value("pull", "unlock-days"))
        if limit and days >= limit:
            raise RunRefused(
                f"Your configuration has been unlocked for {days} days, past `pull: values: unlock-days`: "
                "run `scoter lock` to review your edits."
            )
        warnings.append(
            "You're running your unlocked configuration: your edits haven't been reviewed.  "
            "`scoter lock` reviews and locks them."
        )
        reminder = _days(found_config.optional_int_value("pull", "unlock-reminder-days"))
        if reminder and days >= reminder:
            warnings.append(f"It has been unlocked for {days} days.  It allows:")
            warnings.extend(_listing(run))
    else:
        assert in_use is not None  # nosec: B101  # staged above
        changed = _timers(found_config, link, in_use, run, now, notices, warnings)
        if changed:
            with verified.locked():
                fresh = configs.load_link(verified, project_dir, env_id)
                if fresh is not None:
                    fresh.last_notice = link.last_notice
                    configs.save_link(verified, fresh)

    notice = _project_changed(files_dir, file_name, limits, link)
    if notice:
        notices.append(notice)
    run.set_record(CONFIGURATION, {
        "environment": env_id,
        "id": None if in_use is None else in_use.id,
        "unlocked": link.unlocked is not None,
    })
    return Prepared(run, dest, None if in_use is None else in_use.id, notices, warnings)


def _timers(
    config: UserConfig,
    link: configs.Link,
    in_use: configs.LockedConfig,
    run: UserConfig,
    now: datetime.datetime,
    notices: list[str],
    warnings: list[str],
) -> bool:
    """The review timers (VER-20); whether the notice was given, so the link records it."""
    reviewed = link.last_reviewed or in_use.created_at
    days = (now - reviewed).days
    since = f"You last reviewed this configuration {days} days ago"
    expire = _days(config.optional_int_value("verification", "review-expire-days"))
    if expire and days >= expire:
        raise RunRefused(f"{since}, past `verification: values: review-expire-days`: run `scoter review`.")
    warn = _days(config.optional_int_value("verification", "review-warn-days"))
    if warn and days >= warn:
        warnings.append(f"{since}; `scoter review` looks at it again.  It allows:")
        warnings.extend(_listing(run))
        return False
    first = _days(config.optional_int_value("verification", "review-notice-first-days"))
    every = _days(config.optional_int_value("verification", "review-notice-every-days"))
    if not first:
        return False
    start = reviewed + datetime.timedelta(days=first)
    if now < start:
        return False
    due = start
    if every:
        due = start + datetime.timedelta(days=every * ((now - start).days // every))
    if link.last_notice is not None and link.last_notice >= due:
        return False
    notices.append(f"{since}; `scoter review` looks at it again.  It allows:")
    notices.extend(_listing(run))
    link.last_notice = now
    return True


def _project_changed(
    files_dir: Path, file_name: str, limits: snapshot.SnapshotLimits, link: configs.Link,
) -> str | None:
    """A line when the project's files differ from the last pull's, by hash alone (VER-21)."""
    recorded = jdata.find_str(link.provenance or {}, "source-id")
    if recorded is None or not files_dir.is_dir():
        return None
    try:
        live = snapshot.snapshot_source(files_dir, file_name, limits)
    except (OSError, SurfScoterErr) as err:
        return f"The project's files can't be compared with your last pull: {review.clean(str(err))}"
    if live.manifest.id == recorded:
        return None
    return "The project's files changed since your last pull: `scoter pull` reviews the changes."
