#SPDX:MIT
"""Start and end an exemption on a project (`scoter exempt`).

An exemption is a window in which the project's runs don't stop for a
review: they use an acceptance's settings, project file and `dev-env.yaml`
files, with the container files as they are (see `staging`), on the branch
the exemption was started on.  It's recorded in the environment's record.
"""
import datetime
import os
import re

from ..err import VerifiedStoreErr
from ..user_config import UserConfig
from . import lookup, records, store

DEFAULT_DAYS = 1
DEFAULT_MAX_DAYS = 7
_DURATION = re.compile(r"([0-9]+)([dhm])")
_UNITS = {"d": "days", "h": "hours", "m": "minutes"}


class ExemptErr(VerifiedStoreErr):
    """An exemption can't be started or ended."""


def parse_duration(text: str) -> datetime.timedelta:
    """`1d`, `8h`, `30m`, or a sum of them (`1d12h`)."""
    pos, total = 0, datetime.timedelta()
    for match in _DURATION.finditer(text):
        if match.start() != pos:
            break
        total += datetime.timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})
        pos = match.end()
    if not text or pos != len(text) or total <= datetime.timedelta():
        raise ExemptErr(f"`{text}` isn't a length of time, such as `1d`, `8h` or `30m`")
    return total


def _environment(config: UserConfig) -> tuple[store.VerifiedStore, str, str]:
    project = config.active_location("project-dir")
    verified = config.optional_path("verification", "store")
    if project is None:
        raise ExemptErr("There's no project here.")
    if verified is None or not verified.is_dir():
        raise ExemptErr("You haven't accepted this project's settings: run `scoter verify` first.")
    project_dir = os.path.realpath(project)
    return store.VerifiedStore.open(verified), project_dir, store.project_id(config, project_dir)


def start(
    config: UserConfig, now: datetime.datetime, length: datetime.timedelta | None = None,
) -> records.Exemption:
    """Start (or move the end of) the project's exemption, from the branch's usable acceptance,
    or else the project's newest.

    It lasts `length` (by default `verification: values: exempt-days`), at
    most `max-exempt-days`, and never past the acceptance's expiry.  It
    holds for the run's branch (`scoter:project-branch`), when there is one.
    """
    days = config.optional_int_value("verification", "exempt-days") or DEFAULT_DAYS
    max_days = config.optional_int_value("verification", "max-exempt-days") or DEFAULT_MAX_DAYS
    length = length or datetime.timedelta(days=days)
    if length > datetime.timedelta(days=max_days):
        raise ExemptErr(
            f"An exemption can last at most {max_days} days (`verification: values: max-exempt-days`)."
        )
    verified_store, project_dir, env_id = _environment(config)
    with verified_store.locked():
        record = records.load_environment(verified_store, project_dir, env_id)
        usable = [a for a in (record.acceptances if record else []) if a.usable(now)]
        if record is None or not usable:
            raise ExemptErr(
                "You have no usable acceptance of this project's settings to exempt from: "
                "run `scoter verify` first."
            )
        branch = lookup.project_branch(config)
        acceptance = next((a for a in usable if a.branch == branch), usable[0])
        record.exemption = records.Exemption(
            acceptance=acceptance.id,
            until=min(now + length, acceptance.expires_at),
            branch=branch,
            started_at=now,
        )
        records.save_environment(verified_store, record)
        return record.exemption


def end(config: UserConfig) -> records.Exemption | None:
    """End the project's exemption now; the one ended, or None without one."""
    verified_store, project_dir, env_id = _environment(config)
    with verified_store.locked():
        record = records.load_environment(verified_store, project_dir, env_id)
        if record is None or record.exemption is None:
            return None
        ended = record.exemption
        record.exemption = None
        records.save_environment(verified_store, record)
        return ended


def current(config: UserConfig, now: datetime.datetime) -> records.Exemption | None:
    """The project's exemption, if one holds for the run's branch now."""
    try:
        verified_store, project_dir, env_id = _environment(config)
    except ExemptErr:
        return None
    record = records.load_environment(verified_store, project_dir, env_id)
    if record is None or record.exempt_acceptance(lookup.project_branch(config), now) is None:
        return None
    return record.exemption

