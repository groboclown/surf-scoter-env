"""Maintains an inventory of authorized and fingerprinted projects.

This does not examine the files for correctness or safety.
"""
import datetime
import json
from collections.abc import Iterable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

from .. import jdata
from ..err import ProjectAuthorizationFormatErr

SCHEMA_VERSION_V1 = "1"


class RecordValidity(StrEnum):
    """How to consider the current valid state of the record."""
    VALID = "valid"

    # Outside the valid range.
    EXPIRED = "expired"

    # The user explicitly exempted this record from checks
    # until a future time.
    EXEMPT = "exempt"

    # The window itself has a problem.
    INVALID = "invalid"


class RecordWindow:
    """A window of time for which the record is valid.
    
    A record has several traits that all go into considering
    a validity window.

    One other item goes into the exemption check - whether
    a sufficiently large amount of fingerprints exist *after*
    this one.  That check belongs in the ProjectSet, as that
    has access to the history of authorized project fingerprints.
    """

    def __init__(
        self,
        start: datetime.datetime,
        expires: datetime.datetime,
        exempt_until: datetime.datetime | None,
    ) -> None:
        self._start = start
        self.expires = expires
        self.exempt_until = exempt_until

    @property
    def start(self) -> datetime.datetime:
        """When the record was created."""
        return self._start

    def validity(self, now: datetime.datetime | None = None) -> RecordValidity:
        """Determine the validity of this window."""
        if not now:
            now = datetime.datetime.now(datetime.UTC)
        if now < self.start:
            # Now is before the fingerprint took place.  We don't allow time travelers.
            return RecordValidity.INVALID
        if self.expires < now:
            # Expiration time happened before right now.
            return RecordValidity.EXPIRED
        if self.exempt_until and now < self.exempt_until:
            # Right now is within the exemption time window.
            return RecordValidity.EXEMPT
        return RecordValidity.VALID

    def to_json(self) -> jdata.JsonData:
        """Convert this into a JSON data type."""
        return {
            "start": self.start.isoformat(),
            "expires": self.expires.isoformat(),
            "exempt_until": None if self.exempt_until is None else self.exempt_until.isoformat(),
        }

    @staticmethod
    def from_json_v1(data: jdata.JsonData) -> "RecordWindow":
        """Extract from JSON data."""
        exempt_until: datetime.datetime | None = None
        exempt_until_str = jdata.opt_str(data, "exempt_until")
        if exempt_until_str:
            exempt_until = datetime.datetime.fromisoformat(exempt_until_str)

        return RecordWindow(
            start=datetime.datetime.fromisoformat(jdata.req_str("start")),
            expires=datetime.datetime.fromisoformat(jdata.req_str("expires")),
            exempt_until=exempt_until,
        )


class Fingerprint(NamedTuple):
    """A specific fingerprint + algorithm."""

    fingerprint: str
    algorithm: str

    def to_json(self) -> jdata.JsonData:
        """Convert this into a JSON data type."""
        return [self.algorithm, self.fingerprint]

    @staticmethod
    def from_json_v1(data: jdata.JsonData) -> "Fingerprint":
        """Convert this data from JSON data."""
        return Fingerprint(
            algorithm=jdata.req_str(data, 0),
            fingerprint=jdata.req_str(data, 1),
        )


class AlgorithmMatch(NamedTuple):
    """Describes the result of a fingerprint algorithm match."""

    algorithm: str
    source: str | None
    target: str | None


class ResourceFingerprint(NamedTuple):
    """Fingerprints a resource.
    
    This leaves the actual fingerprint method up to the fingerprint handler.
    """

    resource: str
    fingerprints: Sequence[Fingerprint]

    def match(self, fingerprints: Sequence[Fingerprint]) -> tuple[list[AlgorithmMatch], list[AlgorithmMatch]]:
        """Return the fingerprints that match and don't match."""
        yes: list[AlgorithmMatch] = []
        no: list[AlgorithmMatch] = []
        extra = list(fingerprints)
        for fin in self.fingerprints:
            missing = True
            for idx in range(len(extra)):
                if fin.algorithm == extra[idx].algorithm:
                    match = AlgorithmMatch(fin.algorithm, fin.fingerprint, extra[idx].fingerprint)
                    if match.source == match.target:
                        yes.append(match)
                    else:
                        no.append(match)
                    extra = [*extra[:idx], *extra[idx+1:]]
                    missing = False
                    break
            if missing:
                no.append(AlgorithmMatch(fin.algorithm, fin.fingerprint, None))
        for fin in extra:
            no.append(AlgorithmMatch(fin.algorithm, None, fin.fingerprint))
        return (yes, no)

    def to_json(self) -> jdata.JsonData:
        """Convert this into a JSON data type."""
        return {
            "rc": self.resource,
            "fg": [f.to_json() for f in self.fingerprints],
        }

    @staticmethod
    def from_json_v1(data: jdata.JsonData) -> "ResourceFingerprint":
        """Convert this data from JSON data."""
        return ResourceFingerprint(
            resource=jdata.req_str(data, "rc"),
            fingerprints=[
                Fingerprint.from_json_v1(f)
                for f in jdata.req_list(data, "fg")
            ],
        )


class RecordMatchResult(NamedTuple):
    """The result of a match on the resources' fingerprints in a record.
    
    This is viewed from the source record's perspective of differences.
    """

    # Matched resources.
    match: dict[str, ResourceFingerprint]

    # Mismatched resources.
    mismatch: dict[str, list[AlgorithmMatch]]

    # Resources that were present in the source but the target didn't have.
    missing_resources: dict[str, ResourceFingerprint]

    # Resources that the target provided but the source didn't have.
    extra_resources: dict[str, ResourceFingerprint]

    def is_exact(self) -> bool:
        """Check if this is an exact match."""
        return (
            # no 'match' along with these rules means there were no sources,
            # but allow that corner case.
            not self.mismatch
            and not self.missing_resources
            and not self.extra_resources
        )


class ProjectAuthorizationRecord:
    """A single project's authorization record."""

    def __init__(self, fingerprints: Iterable[ResourceFingerprint], window: RecordWindow) -> None:
        # Construction makes no judgement about the validity of the window.
        self.window = window
        self.fingerprints: dict[str, ResourceFingerprint] = {}
        for fin in fingerprints:
            if fin.resource in self.fingerprints:
                raise ValueError(f"duplicate resource fingerprint: {fin.resource}")
            self.fingerprints[fin.resource] = fin

    def match(self, fingerprints: Iterable[ResourceFingerprint]) -> RecordMatchResult:
        """Return matched and not-matched fingerprints."""
        inp: dict[str, ResourceFingerprint] = {
            f.resource: f
            for f in fingerprints
        }
        yes: dict[str, ResourceFingerprint] = {}
        mismatch: dict[str, list[AlgorithmMatch]] = {}
        missing_resources: dict[str, ResourceFingerprint] = {}
        for key,fin in self.fingerprints.items():
            target = inp.get(key)
            if not target:
                missing_resources[key] = fin
                continue
            del inp[key]
            matched, wrong = fin.match(target.fingerprints)
            if wrong:
                mismatch[key] = [*matched, *wrong]
            else:
                yes[key] = fin
        return RecordMatchResult(
            match=yes,
            mismatch=mismatch,
            missing_resources=missing_resources,
            extra_resources=inp,
        )

    def validity(self, now: datetime.datetime | None) -> RecordValidity:
        """Check the validity of this record against the given time."""
        return self.window.validity(now)

    def to_json(self) -> jdata.JsonData:
        """Convert to JSON data."""
        return {
            "window": self.window.to_json(),
            "fingerprints": [
                f.to_json()
                for f in self.fingerprints.values()
            ],
        }

    @staticmethod
    def from_json_v1(data: jdata.JsonData) -> "ProjectAuthorizationRecord":
        """Extract v1 format data."""
        return ProjectAuthorizationRecord(
            window=RecordWindow.from_json_v1(jdata.optional(data, "window")),
            fingerprints=[
                ResourceFingerprint.from_json_v1(r)
                for r in jdata.req_list(data, "fingerprints")
            ],
        )


class ProjectSet:
    """A collection of authorization records for a single project directory.
    
    The project also has a "current" in-use host list (the exact format will
    come with feature #4) that the user authorized.  That has a 
    """

    def __init__(
        self,
        path: Path,
        records: list[ProjectAuthorizationRecord],
    ) -> None:
        self.path = path.absolute()
        self.records = records

    def add(
        self,
        fingerprints: Iterable[ResourceFingerprint],
        expiration: datetime.timedelta,
        now: datetime.datetime | None = None,
    ) -> None:
        """Add the fingerprints to the record for this path."""
        now = now or datetime.datetime.now(datetime.UTC)
        expires = now + expiration
        if expires < now:
            raise ValueError("cannot expire in the past")
        self.records.insert(0, ProjectAuthorizationRecord(
            fingerprints,
            window=RecordWindow(
                start=now,
                expires=expires,
                exempt_until=None,
            ),
        ))

    def match(
        self, current_state: Iterable[ResourceFingerprint]
    ) -> list[ProjectAuthorizationRecord]:
        """Get the authorized records that match the current state.
        
        This returns the most recent construction first.
        """
        ret: list[ProjectAuthorizationRecord] = []
        for rec in self.records:
            match = rec.match(current_state)
            if match.is_exact():
                ret.append(rec)
        sorted(ret, key=lambda x: x.window.start, reverse=True)
        return ret

    def to_json(self) -> jdata.JsonData:
        """Convert to JSON data."""
        return {
            "path": str(self.path),
            "records": [r.to_json() for r in self.records]
        }

    @staticmethod
    def from_json_v1(data: jdata.JsonData) -> "ProjectSet":
        """Extract v1 format data."""
        return ProjectSet(
            path=Path(jdata.req_str("path")),
            records=[
                ProjectAuthorizationRecord.from_json_v1(r)
                for r in jdata.req_list(data, "records")
            ],
        )


class ProjectAuthorizationSet:
    """Holds all the project authorization entries."""
    def __init__(
        self,
        *,
        source: str | Path,
        records: dict[str, ProjectSet],
    ) -> None:
        self.source = source
        self.records = records

    def to_json(self) -> jdata.JsonData:
        """Export to JSON in the current version."""
        return {
            "$schema": SCHEMA_VERSION_V1,
            "records": {
                key: val.to_json()
                for key,val in self.records.items()
            }
        }

    @staticmethod
    def from_json(source: str | Path, data: jdata.JsonData) -> "ProjectAuthorizationSet":
        """Convert the JSON-like data into an ProjectAuthorizationSet."""
        try:
            schema_version = jdata.opt_str(data, "$schema")
            if schema_version == SCHEMA_VERSION_V1:
                return ProjectAuthorizationSet._from_v1(source, data)
            raise ProjectAuthorizationFormatErr(
                f"Project authorization file {source} format error: invalid file schema"
            )
        except (json.JSONDecodeError, TypeError, ValueError) as err:
            raise ProjectAuthorizationFormatErr(
                f"Project authorization file {source} format error: {err}"
            ) from err

    @staticmethod
    def _from_v1(source: str | Path, data: jdata.JsonData) -> "ProjectAuthorizationSet":
        """Convert the file from a v1 format."""
        return ProjectAuthorizationSet(
            source=source,
            records={
                key: ProjectSet.from_json_v1(val)
                for key, val in jdata.req_dict(data, "records").items()
            },
        )
