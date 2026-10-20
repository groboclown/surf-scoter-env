#SPDX:MIT
"""Each environment's acceptances: `ENV-ID/environment.json` in the verified store.

An acceptance records what the user accepted, and what it holds for:

* `suggestion`: the manifest of the project's files it was reviewed from;
* `setup`: the setup values it was reviewed under (see `setup_values`);
* its window: when it was accepted, when it expires, and any exemption.

A run looks for an acceptance with the same `setup` id whose `suggestion`
matches the project's files now: exactly, or on meaning (the same but for
comments and the like), which carries it forward.  A `blanket` acceptance
has no suggestion and matches any.

See docs/contributors/projects-v1.md, step 5.
"""
import datetime
import secrets
from dataclasses import dataclass, replace
from enum import StrEnum

from .. import jdata
from ..err import VerifiedStoreErr
from .snapshot import Manifest
from .store import VerifiedStore, json_id, nullable_dict, nullable_str

ENVIRONMENT_SCHEMA = "surf-scoter-verified-environment-1"
ENVIRONMENT_FILE = "environment.json"
ACCEPTANCES_DIR = "acceptances"


class Kind(StrEnum):
    """How an acceptance came about."""

    # The user reviewed the project's files.
    REVIEWED = "reviewed"
    # An earlier acceptance, for files that differ from it only in noise.
    CARRIED = "carried"
    # The user accepts the project whatever its files say.
    BLANKET = "blanket"


class Validity(StrEnum):
    """Whether an acceptance can be used now."""

    VALID = "valid"
    EXPIRED = "expired"
    # Exempted from its checks until a later time (step 20).
    EXEMPT = "exempt"
    # The window itself is wrong: it starts in the future, say.
    INVALID = "invalid"


def _time(text: str) -> datetime.datetime:
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
    suggestion: Manifest | None
    from_id: str | None = None
    exempt_until: datetime.datetime | None = None
    branch: str | None = None

    def validity(self, now: datetime.datetime) -> Validity:
        """Whether it can be used at `now`."""
        if now < self.accepted_at or self.expires_at < self.accepted_at:
            return Validity.INVALID
        if self.expires_at <= now:
            return Validity.EXPIRED
        if self.exempt_until is not None and now < self.exempt_until:
            return Validity.EXEMPT
        return Validity.VALID

    def usable(self, now: datetime.datetime) -> bool:
        """Valid, or exempt."""
        return self.validity(now) in (Validity.VALID, Validity.EXEMPT)

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data."""
        return {
            "id": self.id,
            "kind": str(self.kind),
            "from": self.from_id,
            "accepted-at": _text(self.accepted_at),
            "expires-at": _text(self.expires_at),
            "exempt-until": _text(self.exempt_until),
            "branch": self.branch,
            "suggestion": None if self.suggestion is None else self.suggestion.to_json(),
            "setup": self.setup.to_json(),
        }

    @staticmethod
    def from_json(data: jdata.JsonData) -> "Acceptance":
        """From JSON data."""
        kind = Kind(jdata.req_str(data, "kind"))
        suggestion_data = nullable_dict(data, "suggestion")
        suggestion = None if suggestion_data is None else Manifest.from_json(suggestion_data)
        if (suggestion is None) != (kind == Kind.BLANKET):
            raise ValueError("suggestion: only a blanket acceptance has none")
        exempt = nullable_str(data, "exempt-until")
        acceptance_id = jdata.req_str(data, "id")
        _check_id(acceptance_id)
        return Acceptance(
            id=acceptance_id,
            kind=kind,
            from_id=nullable_str(data, "from"),
            accepted_at=_time(jdata.req_str(data, "accepted-at")),
            expires_at=_time(jdata.req_str(data, "expires-at")),
            exempt_until=None if exempt is None else _time(exempt),
            branch=nullable_str(data, "branch"),
            suggestion=suggestion,
            setup=SetupRecord.from_json(jdata.req_dict(data, "setup")),
        )


def new_acceptance_id(now: datetime.datetime) -> str:
    """A new acceptance id: the time, and a random part."""
    return now.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)


def _check_id(acceptance_id: str) -> None:
    if not acceptance_id or not all(c.isalnum() or c == "-" for c in acceptance_id):
        raise ValueError(f"`{acceptance_id}` is not an acceptance id")


class MatchKind(StrEnum):
    """How an acceptance matched the project's files."""

    EXACT = "exact"
    MEANING = "meaning"
    BLANKET = "blanket"


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

    def find(self, current: Manifest, setup: SetupRecord, now: datetime.datetime) -> Match | None:
        """The acceptance that holds for the project's files now, if any.

        An exact match first, then one on meaning, then a blanket acceptance;
        the newest of each.  Only usable acceptances under the same setup count.
        """
        usable = [
            a for a in self.acceptances
            if a.usable(now) and a.setup.id == setup.id
        ]
        for acceptance in usable:
            if acceptance.suggestion is not None and acceptance.suggestion.id == current.id:
                return Match(acceptance, MatchKind.EXACT)
        for acceptance in usable:
            if (
                acceptance.suggestion is not None
                and acceptance.suggestion.meaning_id == current.meaning_id
            ):
                return Match(acceptance, MatchKind.MEANING)
        for acceptance in usable:
            if acceptance.kind == Kind.BLANKET:
                return Match(acceptance, MatchKind.BLANKET)
        return None

    def add(self, acceptance: Acceptance) -> None:
        """Add the acceptance, as the newest."""
        if any(a.id == acceptance.id for a in self.acceptances):
            raise VerifiedStoreErr(f"{self.id}: already has an acceptance `{acceptance.id}`")
        self.acceptances.insert(0, acceptance)

    def carry_forward(
        self, match: Match, current: Manifest, now: datetime.datetime, branch: str | None,
    ) -> Acceptance:
        """Add an acceptance for `current`, carried from a match on meaning.

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
        self.add(carried)
        return carried

    def prune(self, keep: int, now: datetime.datetime) -> list[Acceptance]:
        """Remove the expired (or invalid) acceptances beyond the newest `keep`; those removed."""
        kept: list[Acceptance] = []
        removed: list[Acceptance] = []
        unusable = 0
        for acceptance in self.acceptances:
            if acceptance.usable(now):
                kept.append(acceptance)
                continue
            unusable += 1
            (kept if unusable <= keep else removed).append(acceptance)
        self.acceptances = kept
        return removed

    def blobs(self) -> set[str]:
        """The blobs its acceptances' suggestions refer to."""
        return {
            entry.sha256
            for acceptance in self.acceptances
            if acceptance.suggestion is not None
            for entry in acceptance.suggestion.files.values()
        }

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data."""
        return {
            "$schema": ENVIRONMENT_SCHEMA,
            "id": self.id,
            "project-dir": self.project_dir,
            "acceptances": [a.to_json() for a in self.acceptances],
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


def save_environment(store: VerifiedStore, record: EnvironmentRecord, keep: int) -> list[Acceptance]:
    """Write the environment's record, after pruning it; the acceptances pruned.

    The store must be locked.  Pruned acceptances' files go, and so do the
    blobs no environment refers to any more.
    """
    removed = record.prune(keep, now_utc())
    store.register(record.project_dir, record.id)
    store.write_json(record.id, ENVIRONMENT_FILE, data=record.to_json())
    for acceptance in removed:
        store.remove(record.id, ACCEPTANCES_DIR, acceptance.id)
    if removed:
        remove_unused_blobs(store)
    return removed


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
    suc: jdata.DictJsonData,
    vcs_section: str | None,
    verified_containers: dict[str, str],
) -> jdata.DictJsonData:
    """The setup values from the SUC that an acceptance holds for.

    A change to any of them asks for a new review: they're the boundaries
    the project was reviewed within, and they come from the setup files, not
    the project.  `verified_containers` maps each `dev-containers-verified`
    container directory the build uses to its manifest's id.
    """
    scoter = nullable_dict(suc, "resolved", "scoter") or {}
    paths = nullable_dict(scoter, "paths") or {}
    path_lists = nullable_dict(scoter, "path-lists") or {}
    values = nullable_dict(scoter, "values") or {}
    vcs_runs: jdata.JsonData = None
    if vcs_section:
        runs = nullable_dict(suc, "resolved", vcs_section, "runs") or {}
        vcs_runs = {
            key: runs.get(key)
            for key in ("local-clone", "fetch-changes-into", "show-fetch-changes", "merge-changes")
        }
    checks = nullable_dict(suc, "host-command-checks") or {}
    return {
        "project-files-dir": values.get("project-files-dir"),
        "dev-containers-search": path_lists.get("dev-containers-search"),
        "dev-containers-verified": path_lists.get("dev-containers-verified"),
        "verified-dir": paths.get("verified-dir"),
        "vcs": vcs_section,
        "vcs-runs": vcs_runs,
        # What enforces the egress rules: a different gateway enforces them
        # differently, so the rules need a new review.
        "gateway": {
            "name": (nullable_dict(suc, "resolved", "network-gateway", "values") or {}).get("name"),
            "write-config":
                (nullable_dict(suc, "resolved", "network-gateway", "runs") or {}).get("write-config"),
        },
        "host-command-checks": checks.get("checks"),
        "hidden-host-commands": _hidden_checks(suc),
        "verified-containers": dict(sorted(verified_containers.items())),
    }


def _hidden_checks(suc: jdata.DictJsonData) -> jdata.DictJsonData:
    """The lower priority versions of each `host-commands` check a setup file replaces or removes.

    An upgrade that changes one of them doesn't change the check in effect,
    but should still ask for a new review.
    """
    versions: dict[str, list[jdata.JsonData]] = {}
    setup = nullable_dict(suc, "setup") or {}
    files = setup.get("files") or []
    if not isinstance(files, list):
        raise TypeError("setup.files: expected a list")
    for file in files:
        sections = nullable_dict(file, "sections") or {}
        for section, groups in sections.items():
            checks = nullable_dict(groups, "host-commands") or {}
            for key, check in checks.items():
                versions.setdefault(f"{section}:{key}", []).append(
                    {"file": nullable_str(file, "file"), "check": check}
                )
    return {key: found[1:] for key, found in sorted(versions.items()) if len(found) > 1}
