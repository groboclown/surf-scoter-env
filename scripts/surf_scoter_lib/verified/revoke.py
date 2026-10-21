#SPDX:MIT
"""List and revoke the user's acceptances of projects' settings.

`main_list_accepted.py` and `main_revoke.py` call this.  A revoked
acceptance is removed from its environment's record, with its files, and
the blobs nothing else refers to go too; the next run of the project asks
for a review again.  An environment left with no acceptances is removed,
and taken out of the store's index.

Acceptances are chosen:

* by their id, as `scoter list-accepted` shows it, for any project;
* or, for the current project, by the branch they were accepted on, or by
  the fingerprint (the snapshot id) of the files they were accepted for, as
  the review file shows it.

An acceptance carried forward from a chosen one (step 5: the same files,
but for comments) holds the same settings, so it goes with it.
"""
import datetime
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from ..user_config import UserConfig
from . import records, store

# The shortest fingerprint prefix taken: as long as the review shows them.
MIN_FINGERPRINT = 12
FINGERPRINT_PREFIX = "sha256:"
# An acceptance id, as `records.new_acceptance_id` makes them.
ACCEPTANCE_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")


@dataclass
class Selection:
    """The acceptances chosen, and the names that chose none."""

    chosen: list[records.Acceptance] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Environment:
    """A project's place in the verified store."""

    store: store.VerifiedStore
    project_dir: str
    env_id: str

    def record(self) -> records.EnvironmentRecord | None:
        """The environment's record; None without any acceptances."""
        return records.load_environment(self.store, self.project_dir, self.env_id)


def open_store(config: UserConfig) -> store.VerifiedStore | None:
    """The user's verified store; None when there isn't one yet."""
    verified = config.optional_path("verification", "store")
    if verified is None or not verified.is_dir():
        return None
    return store.VerifiedStore.open(verified)


def environment(config: UserConfig, verified_store: store.VerifiedStore) -> Environment | None:
    """The current project's environment in the store; None without a project."""
    project = config.active_location("project-dir")
    if project is None:
        return None
    project_dir = os.path.realpath(project)
    return Environment(verified_store, project_dir, store.project_id(config, project_dir))


def environments(verified_store: store.VerifiedStore) -> list[Environment]:
    """Every environment in the store's index, by project directory."""
    return [
        Environment(verified_store, project_dir, env_id)
        for project_dir, env_id in sorted(verified_store.index().items())
    ]


def fingerprint(acceptance: records.Acceptance) -> str | None:
    """The acceptance's fingerprint: its snapshot's id; None for a blanket acceptance."""
    return None if acceptance.suggestion is None else acceptance.suggestion.id


def describe(acceptance: records.Acceptance, now: datetime.datetime) -> str:
    """One line for the acceptance: its id, kind, branch, fingerprint, expiry and exemption."""
    found = fingerprint(acceptance)
    short = found.removeprefix(FINGERPRINT_PREFIX)[:MIN_FINGERPRINT] if found else "-"
    exempt = (
        f"  exempt until {acceptance.exempt_until.isoformat()}" if acceptance.exempt_until else ""
    )
    carried = f"  carried from {acceptance.from_id}" if acceptance.from_id else ""
    return (
        f"{acceptance.id}  {acceptance.kind:<8}  branch {acceptance.branch or '-'}  "
        f"fingerprint {short}  expires {acceptance.expires_at.isoformat()} "
        f"({acceptance.validity(now)}){exempt}{carried}"
    )


def _matches_fingerprint(name: str, acceptance: records.Acceptance) -> bool:
    found = fingerprint(acceptance)
    if found is None:
        return False
    digest = name.removeprefix(FINGERPRINT_PREFIX).lower()
    return (
        len(digest) >= MIN_FINGERPRINT
        and all(c in "0123456789abcdef" for c in digest)
        and found.removeprefix(FINGERPRINT_PREFIX).startswith(digest)
    )


def with_carried(record: records.EnvironmentRecord, ids: Iterable[str]) -> list[records.Acceptance]:
    """The acceptances `ids`, with those carried forward from them, in the record's order."""
    chosen = set(ids)
    added = True
    while added:
        added = False
        for acceptance in record.acceptances:
            if acceptance.id not in chosen and acceptance.from_id in chosen:
                chosen.add(acceptance.id)
                added = True
    return [a for a in record.acceptances if a.id in chosen]


def select(
    record: records.EnvironmentRecord, names: Iterable[str], *, everything: bool = False,
) -> Selection:
    """The acceptances `names` choose: each an id, a branch, or a fingerprint (or the start of one).

    With `everything`, all of them.  Acceptances carried forward from a
    chosen one are chosen too.
    """
    if everything:
        return Selection(list(record.acceptances))
    chosen: set[str] = set()
    selection = Selection()
    for name in names:
        found = [
            a.id for a in record.acceptances
            if a.id == name or a.branch == name or _matches_fingerprint(name, a)
        ]
        if not found:
            selection.unmatched.append(name)
        chosen.update(found)
    selection.chosen = with_carried(record, chosen)
    return selection


def find_ids(
    verified_store: store.VerifiedStore, ids: Iterable[str],
) -> dict[Environment, list[records.Acceptance]]:
    """The acceptances with these ids, in any project, by environment (with those carried from them)."""
    wanted = {i for i in ids if ACCEPTANCE_ID.fullmatch(i)}
    found: dict[Environment, list[records.Acceptance]] = {}
    if not wanted:
        return found
    for env in environments(verified_store):
        record = env.record()
        if record is None:
            continue
        matched = [a.id for a in record.acceptances if a.id in wanted]
        if matched:
            found[env] = with_carried(record, matched)
    return found


def revoke(env: Environment, ids: Iterable[str]) -> list[records.Acceptance]:
    """Remove the acceptances `ids` from the project's record, with their files; those removed.

    A project left with none is removed from the store, and its index.
    """
    wanted = set(ids)
    with env.store.locked():
        # Again, under the lock: another run may have changed it.
        record = env.record()
        if record is None:
            return []
        removed = [a for a in record.acceptances if a.id in wanted]
        if not removed:
            return []
        record.acceptances = [a for a in record.acceptances if a.id not in wanted]
        if record.acceptances:
            # Written before the files go, so the record never names a missing acceptance.
            env.store.write_json(env.env_id, records.ENVIRONMENT_FILE, data=record.to_json())
            for acceptance in removed:
                env.store.remove(env.env_id, records.ACCEPTANCES_DIR, acceptance.id)
        else:
            # Out of the index first, so the index never names a missing environment.
            env.store.unregister(env.project_dir)
            env.store.remove(env.env_id)
        records.remove_unused_blobs(env.store)
    return removed
