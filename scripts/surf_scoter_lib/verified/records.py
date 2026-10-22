#SPDX:MIT
"""Each environment's acceptances: `ENV-ID/environment.json` in the verified store.

An acceptance records what the user accepted, and what it holds for:

* `suggestion`: the manifest of the project's files it was reviewed from;
* `setup`: the setup values it was reviewed under (see `setup_values`);
* its window: when it was accepted, and when it expires.

A run looks for an acceptance with the same `setup` id whose `suggestion`
matches the project's files now: exactly, or on meaning (the same but for
comments and the like), which carries it forward.  Failing those, an
exemption the user started (`scoter exempt`) on the environment
holds for its branch: the run then uses the exemption's acceptance, with
the container files as they are.

An environment keeps one acceptance per branch (`branch`, None without
one): a new one replaces the branch's last, which is what the next review
compares with, so nothing piles up.
"""
import datetime
import secrets
from dataclasses import dataclass, replace
from enum import StrEnum

from .. import jdata
from ..err import VerifiedStoreErr
from ..user_config import UserConfig
from .snapshot import Manifest
from .store import VerifiedStore, json_id

ENVIRONMENT_SCHEMA = "surf-scoter-verified-environment-1"
ENVIRONMENT_FILE = "environment.json"
ACCEPTANCES_DIR = "acceptances"
# The random part of an acceptance id, in bytes (twice as many hex digits).
ACCEPTANCE_ID_RANDOM_BYTES = 4


class Kind(StrEnum):
    """How an acceptance came about."""

    # The user reviewed the project's files.
    REVIEWED = "reviewed"
    # An earlier acceptance, for files that differ from it only in noise.
    CARRIED = "carried"
    # The project's suggestion, accepted as it stood, unreviewed, under an
    # exemption (`scoter verify --with-exemption`).
    EXEMPTED = "exempted"


class Validity(StrEnum):
    """Whether an acceptance can be used now."""

    VALID = "valid"
    EXPIRED = "expired"
    # The window itself is wrong: it starts in the future, say.
    INVALID = "invalid"


def parse_time(text: str) -> datetime.datetime:
    """An ISO 8601 (RFC 3339) time, which must have a time zone."""
    value = datetime.datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise ValueError(f"`{text}` has no time zone")
    return value


def _text(value: datetime.datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def now_utc() -> datetime.datetime:
    """Now, in UTC, to the second."""
    return datetime.datetime.now(datetime.UTC).replace(microsecond=0)


@dataclass(frozen=True)
class SetupRecord:
    """The setup values an acceptance was reviewed under, and their id."""

    values: jdata.DictJsonData

    @property
    def id(self) -> str:
        """The hash of the values."""
        return json_id(self.values)

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data, with its id."""
        return {"id": self.id, "values": self.values}

    @staticmethod
    def from_json(data: jdata.JsonData) -> "SetupRecord":
        """From JSON data, checking the id."""
        record = SetupRecord(jdata.req_dict(data, "values"))
        if jdata.req_str(data, "id") != record.id:
            raise ValueError("setup.id: doesn't match its values")
        return record


@dataclass(frozen=True)
class Acceptance:
    """One acceptance of an environment's settings."""

    id: str
    kind: Kind
    accepted_at: datetime.datetime
    expires_at: datetime.datetime
    setup: SetupRecord
    suggestion: Manifest
    from_id: str | None = None
    branch: str | None = None

    def validity(self, now: datetime.datetime) -> Validity:
        """Whether it can be used at `now`."""
        if now < self.accepted_at or self.expires_at < self.accepted_at:
            return Validity.INVALID
        if self.expires_at <= now:
            return Validity.EXPIRED
        return Validity.VALID

    def usable(self, now: datetime.datetime) -> bool:
        """Valid at `now`."""
        return self.validity(now) == Validity.VALID

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data."""
        return {
            "id": self.id,
            "kind": str(self.kind),
            "from": self.from_id,
            "accepted-at": _text(self.accepted_at),
            "expires-at": _text(self.expires_at),
            "branch": self.branch,
            "suggestion": self.suggestion.to_json(),
            "setup": self.setup.to_json(),
        }

    @staticmethod
    def from_json(data: jdata.JsonData) -> "Acceptance":
        """From JSON data."""
        kind = Kind(jdata.req_str(data, "kind"))
        suggestion = Manifest.from_json(jdata.req_dict(data, "suggestion"))
        acceptance_id = jdata.req_str(data, "id")
        _check_id(acceptance_id)
        return Acceptance(
            id=acceptance_id,
            kind=kind,
            from_id=jdata.find_str(data, "from"),
            accepted_at=parse_time(jdata.req_str(data, "accepted-at")),
            expires_at=parse_time(jdata.req_str(data, "expires-at")),
            branch=jdata.find_str(data, "branch"),
            suggestion=suggestion,
            setup=SetupRecord.from_json(jdata.req_dict(data, "setup")),
        )


def new_acceptance_id(now: datetime.datetime) -> str:
    """A new acceptance id: the time, and a random part."""
    return now.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(ACCEPTANCE_ID_RANDOM_BYTES)


def _check_id(acceptance_id: str) -> None:
    if not acceptance_id or not all(c.isalnum() or c == "-" for c in acceptance_id):
        raise ValueError(f"`{acceptance_id}` is not an acceptance id")


class MatchKind(StrEnum):
    """How an acceptance matched the project's files."""

    EXACT = "exact"
    MEANING = "meaning"
    # No match, but an exemption holds: its acceptance, with the container
    # files as they are.
    EXEMPT = "exempt"


@dataclass(frozen=True)
class Exemption:
    """A window in which the environment's runs don't stop for a review.

    It holds for `branch` (when the VCS gave one), until `until`, and uses
    the acceptance `acceptance`: its settings, its project file and
    `dev-env.yaml` files, with the container files as they are.
    """

    acceptance: str
    until: datetime.datetime
    branch: str | None
    started_at: datetime.datetime

    def holds(self, branch: str | None, now: datetime.datetime) -> bool:
        """Whether it holds for a run on `branch` at `now`."""
        return now < self.until and (self.branch is None or self.branch == branch)

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data."""
        return {
            "acceptance": self.acceptance,
            "until": _text(self.until),
            "branch": self.branch,
            "started-at": _text(self.started_at),
        }

    @staticmethod
    def from_json(data: jdata.JsonData) -> "Exemption":
        """From JSON data."""
        acceptance_id = jdata.req_str(data, "acceptance")
        _check_id(acceptance_id)
        return Exemption(
            acceptance=acceptance_id,
            until=parse_time(jdata.req_str(data, "until")),
            branch=jdata.find_str(data, "branch"),
            started_at=parse_time(jdata.req_str(data, "started-at")),
        )


@dataclass(frozen=True)
class Match:
    """The acceptance that holds for the project's files, and how."""

    acceptance: Acceptance
    kind: MatchKind


@dataclass
class EnvironmentRecord:
    """An environment's acceptances, newest first."""

    id: str
    project_dir: str
    acceptances: list[Acceptance]
    exemption: Exemption | None = None

    def exempt_acceptance(self, branch: str | None, now: datetime.datetime) -> Acceptance | None:
        """The acceptance an exemption that holds for `branch` at `now` uses; None without one."""
        if self.exemption is None or not self.exemption.holds(branch, now):
            return None
        found = next((a for a in self.acceptances if a.id == self.exemption.acceptance), None)
        return found if found is not None and found.usable(now) else None

    def find(
        self, current: Manifest, setup: SetupRecord, now: datetime.datetime,
        branch: str | None = None,
    ) -> Match | None:
        """The acceptance that holds for the project's files now, if any.

        An exact match first, then one on meaning, the newest of each; then
        the acceptance an exemption for `branch` uses.  Only usable
        acceptances under the same setup count: an exemption doesn't cover a
        change to the setup.
        """
        usable = [
            a for a in self.acceptances
            if a.usable(now) and a.setup.id == setup.id
        ]
        for acceptance in usable:
            if acceptance.suggestion.id == current.id:
                return Match(acceptance, MatchKind.EXACT)
        for acceptance in usable:
            if acceptance.suggestion.meaning_id == current.meaning_id:
                return Match(acceptance, MatchKind.MEANING)
        exempt = self.exempt_acceptance(branch, now)
        if exempt is not None and exempt.setup.id == setup.id:
            return Match(exempt, MatchKind.EXEMPT)
        return None

    def add(self, acceptance: Acceptance) -> list[Acceptance]:
        """Add the acceptance, as the newest, in place of its branch's others; those replaced.

        An exemption using a replaced acceptance moves to the new one, with
        the same end and branch.  The caller removes the replaced ones'
        files once the record is saved (`remove_replaced`).
        """
        if any(a.id == acceptance.id for a in self.acceptances):
            raise VerifiedStoreErr(f"{self.id}: already has an acceptance `{acceptance.id}`")
        replaced = [a for a in self.acceptances if a.branch == acceptance.branch]
        self.acceptances = [acceptance, *(a for a in self.acceptances if a.branch != acceptance.branch)]
        if self.exemption is not None and any(a.id == self.exemption.acceptance for a in replaced):
            self.exemption = replace(self.exemption, acceptance=acceptance.id)
        return replaced

    def carry_forward(
        self, match: Match, current: Manifest, now: datetime.datetime, branch: str | None,
    ) -> tuple[Acceptance, list[Acceptance]]:
        """Add an acceptance for `current`, carried from a match on meaning; it, and those it replaced.

        It keeps the earlier acceptance's expiry: carrying forward never extends it.
        """
        if match.kind != MatchKind.MEANING:
            raise ValueError("only a match on meaning is carried forward")
        carried = replace(
            match.acceptance,
            id=new_acceptance_id(now),
            kind=Kind.CARRIED,
            from_id=match.acceptance.id,
            accepted_at=now,
            suggestion=current,
            branch=branch,
        )
        return carried, self.add(carried)

    def blobs(self) -> set[str]:
        """The blobs its acceptances' suggestions refer to."""
        return {
            entry.sha256
            for acceptance in self.acceptances
            for entry in acceptance.suggestion.files.values()
        }

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data."""
        return {
            "$schema": ENVIRONMENT_SCHEMA,
            "id": self.id,
            "project-dir": self.project_dir,
            "acceptances": [a.to_json() for a in self.acceptances],
            "exemption": None if self.exemption is None else self.exemption.to_json(),
        }

    @staticmethod
    def from_json(data: jdata.JsonData) -> "EnvironmentRecord":
        """From JSON data."""
        return EnvironmentRecord(
            id=jdata.req_str(data, "id"),
            project_dir=jdata.req_str(data, "project-dir"),
            acceptances=[
                Acceptance.from_json(jdata.req_dict(data, *key))
                for key in jdata.opt_list_keys(data, "acceptances")
            ],
            exemption=(
                None if jdata.find_dict(data, "exemption") is None
                else Exemption.from_json(jdata.req_dict(data, "exemption"))
            ),
        )


# ---------------------------------------------------------------------
# In the store


def load_environment(store: VerifiedStore, project_dir: str, env_id: str) -> EnvironmentRecord | None:
    """The environment's record, or None if it has none.

    The index, the directory name and the record's own `project-dir` must
    agree, so a renamed or swapped directory is refused.
    """
    data = store.read_json(env_id, ENVIRONMENT_FILE, schema=ENVIRONMENT_SCHEMA)
    known = store.index().get(project_dir)
    if data is None:
        if known is not None:
            raise VerifiedStoreErr(
                f"{store.root / env_id}: the index lists {project_dir}, but it has no "
                f"{ENVIRONMENT_FILE}"
            )
        return None
    where = store.root / env_id / ENVIRONMENT_FILE
    try:
        record = EnvironmentRecord.from_json(data)
    except (KeyError, TypeError, ValueError) as err:
        raise VerifiedStoreErr(f"{where}: {err}") from err
    if record.id != env_id or record.project_dir != project_dir:
        raise VerifiedStoreErr(
            f"{where}: is for {record.project_dir} (`{record.id}`), not {project_dir} (`{env_id}`)"
        )
    if known != env_id:
        raise VerifiedStoreErr(f"{where}: the index doesn't list it for {project_dir}")
    return record


def save_environment(store: VerifiedStore, record: EnvironmentRecord) -> None:
    """Write the environment's record, and list it in the index.

    The store must be locked.  Nothing is pruned: an expired acceptance is
    still what the next review compares with, so it stays until
    it's replaced or revoked.
    """
    store.register(record.project_dir, record.id)
    store.write_json(record.id, ENVIRONMENT_FILE, data=record.to_json())


def remove_replaced(store: VerifiedStore, record: EnvironmentRecord, replaced: list[Acceptance]) -> None:
    """Remove the files of the acceptances a new one replaced (`add`), once the record is saved.

    The store must be locked.  The blobs nothing refers to any more go too.
    """
    for acceptance in replaced:
        store.remove(record.id, ACCEPTANCES_DIR, acceptance.id)
    if replaced:
        remove_unused_blobs(store)


def remove_unused_blobs(store: VerifiedStore) -> int:
    """Remove the blobs no environment's acceptances refer to; how many went."""
    used: set[str] = set()
    for project_dir, env_id in store.index().items():
        record = load_environment(store, project_dir, env_id)
        if record is not None:
            used |= record.blobs()
    return store.remove_unused_blobs(used)


# ---------------------------------------------------------------------
# The setup an acceptance holds for


def setup_values(
    config: UserConfig,
    vcs_section: str | None,
    verified_containers: dict[str, str],
) -> jdata.DictJsonData:
    """The setup values from the universal configuration file that an acceptance holds for.

    A change to any of them asks for a new review: they're the boundaries
    the project was reviewed within, and they come from the setup files, not
    the project.  They're kept whole, as the file has them, to compare.
    `verified_containers` maps each `verification: verified-containers` container
    directory the build uses to its manifest's id.
    """
    vcs_runs: jdata.JsonData = None
    if vcs_section:
        vcs_runs = {
            key: config.raw("resolved", vcs_section, "runs", key)
            for key in ("local-clone", "fetch-changes-into", "show-fetch-changes", "merge-changes")
        }
    return {
        "project-files-dir": config.raw("resolved", "scoter", "values", "project-files-dir"),
        "dev-containers-search":
            config.raw("resolved", "scoter", "path-lists", "dev-containers-search"),
        "verified-container-dirs":
            config.raw("resolved", "verification", "path-lists", "verified-containers"),
        "store": config.raw("resolved", "verification", "paths", "store"),
        "vcs": vcs_section,
        "vcs-runs": vcs_runs,
        # What enforces the egress rules: a different gateway enforces them
        # differently, so the rules need a new review.
        "gateway": {
            "name": config.raw("resolved", "dev-env", "values", "network-gateway-name"),
            "write-config": config.raw("resolved", "dev-env", "runs", "network-gateway-write-config"),
        },
        "host-command-checks": config.raw("host-command-checks", "checks"),
        "hidden-host-commands": _hidden_checks(config),
        "verified-containers": dict(sorted(verified_containers.items())),
    }


def _hidden_checks(config: UserConfig) -> jdata.DictJsonData:
    """The lower priority versions of each `host-commands` check a setup file replaces or removes.

    An upgrade that changes one of them doesn't change the check in effect,
    but should still ask for a new review.
    """
    setup = config.raw("setup")
    versions: dict[str, list[jdata.JsonData]] = {}
    for file_path in jdata.find_list_keys(setup, "files"):
        file = jdata.find_str(setup, *file_path, "file")
        for section_path in jdata.find_dict_keys(setup, *file_path, "sections"):
            for key_path in jdata.find_dict_keys(setup, *section_path, "host-commands"):
                versions.setdefault(f"{section_path[-1]}:{key_path[-1]}", []).append(
                    {"file": file, "check": jdata.find(setup, *key_path)}
                )
    return {key: found[1:] for key, found in sorted(versions.items()) if len(found) > 1}


def setup_changes(before: jdata.DictJsonData, now: jdata.DictJsonData) -> list[str]:
    """The setup values that differ: each top-level name, or, within a mapping, `name: key`."""
    changes: list[str] = []
    for name in sorted(set(before) | set(now)):
        old, new = before.get(name), now.get(name)
        if old == new:
            continue
        if isinstance(old, dict) and isinstance(new, dict):
            changes.extend(
                f"{name}: {key}" for key in sorted(set(old) | set(new)) if old.get(key) != new.get(key)
            )
        else:
            changes.append(name)
    return changes
