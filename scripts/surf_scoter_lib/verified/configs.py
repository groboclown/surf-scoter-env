#SPDX:MIT
"""Each environment's locked configurations, and its link, in the verified store.

    ENV-ID/link.json              0600  the environment's link
    ENV-ID/configs/ID.json        0600  each locked configuration

A locked configuration is what a pull, `scoter lock` or `scoter review`
approved: a snapshot (its manifest; the contents are blobs) and its host
file, under a name.  Each environment keeps its own; none is shared.  A
configuration never changes once it's stored: a new approval stores a new
one, with a new id.

The link says what the environment runs: the configuration in use, or the
unlocked copy (and the configuration it was unlocked from).  It also keeps
the base (the snapshot last pulled from, or pushed to, the project), the
provenance of that pull, when the configuration was last reviewed and the
review timers' state, and the configurations waiting for the environments
running them to stop before they're removed (VER-40).  Only `scoter`'s own
commands write it, never a setting or the project.

Names are for people (`scoter switch NAME`); the link points at ids.  At most
one configuration that isn't waiting for removal has each name.
"""
import datetime
import re
import secrets
from collections.abc import Collection
from dataclasses import dataclass, field, replace

from .. import jdata
from ..err import VerifiedStoreErr
from .snapshot import Manifest
from .store import VerifiedStore, json_id, parse_time, time_text

CONFIG_SCHEMA = "surf-scoter-verified-config-1"
LINK_SCHEMA = "surf-scoter-verified-link-1"
LINK_FILE = "link.json"
CONFIGS_DIR = "configs"
CONFIG_SUFFIX = ".json"
DEFAULT_NAME = "default"
# A pull keeps the configuration it replaces under its name with this added.
PREVIOUS_SUFFIX = ".previous"
MAX_NAME_CHARS = 64
# The random part of a configuration id, in bytes (twice as many hex digits).
CONFIG_ID_RANDOM_BYTES = 4

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]+")


def check_name(name: str) -> None:
    """Refuse a configuration name that isn't letters, digits, `.`, `_` and `-`."""
    if not _NAME.fullmatch(name) or len(name) > MAX_NAME_CHARS:
        raise VerifiedStoreErr(
            f"{name!a} is not a configuration name: use up to {MAX_NAME_CHARS} letters, "
            f"digits, `.`, `_` and `-`, starting with a letter or digit"
        )


def check_id(config_id: str) -> None:
    """Refuse a string that isn't a configuration id."""
    if not _ID.fullmatch(config_id):
        raise VerifiedStoreErr(f"{config_id!a} is not a configuration id")


def new_config_id(now: datetime.datetime) -> str:
    """A new configuration id: the time, and a random part."""
    return now.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(CONFIG_ID_RANDOM_BYTES)


@dataclass(frozen=True)
class LockedConfig:
    """A locked configuration: an approved snapshot and its host file."""

    id: str
    name: str
    environment: str
    created_at: datetime.datetime
    snapshot: Manifest
    host_file: jdata.DictJsonData

    @property
    def content_id(self) -> str:
        """The hash of what it holds: the snapshot and the host file, not the name or time."""
        return json_id({"snapshot": self.snapshot.id, "host-file": self.host_file})

    def blobs(self) -> set[str]:
        """The blobs its snapshot refers to."""
        return {entry.sha256 for entry in self.snapshot.files.values()}

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data."""
        return {
            "$schema": CONFIG_SCHEMA,
            "id": self.id,
            "name": self.name,
            "environment": self.environment,
            "created-at": time_text(self.created_at),
            "snapshot": self.snapshot.to_json(),
            "host-file": self.host_file,
        }

    @staticmethod
    def from_json(data: jdata.JsonData) -> "LockedConfig":
        """From JSON data, checking the id, name and snapshot."""
        config_id = jdata.req_str(data, "id")
        check_id(config_id)
        name = jdata.req_str(data, "name")
        check_name(name)
        return LockedConfig(
            id=config_id,
            name=name,
            environment=jdata.req_str(data, "environment"),
            created_at=parse_time(jdata.req_str(data, "created-at")),
            snapshot=Manifest.from_json(jdata.req_dict(data, "snapshot")),
            host_file=jdata.req_dict(data, "host-file"),
        )


@dataclass(frozen=True)
class Unlocked:
    """The environment runs from the unlocked copy.

    `from_id` is the configuration it was unlocked from, which `scoter lock
    --revert` goes back to (None when unlocked from a template, or with no
    configuration).  `template` names the template it started from.  `at`
    is when, which the unlock reminder counts from.
    """

    at: datetime.datetime
    from_id: str | None = None
    template: str | None = None

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data."""
        return {"at": time_text(self.at), "from": self.from_id, "template": self.template}

    @staticmethod
    def from_json(data: jdata.JsonData) -> "Unlocked":
        """From JSON data."""
        from_id = jdata.find_str(data, "from")
        if from_id is not None:
            check_id(from_id)
        return Unlocked(
            at=parse_time(jdata.req_str(data, "at")),
            from_id=from_id,
            template=jdata.find_str(data, "template"),
        )


@dataclass
class Link:
    """What an environment runs, and its pull and review state.

    At most one of `in_use` and `unlocked` is set; neither, for an
    environment with nothing approved yet.
    """

    id: str
    project_dir: str
    in_use: str | None = None
    unlocked: Unlocked | None = None
    # The snapshot last pulled from, or pushed to, the project.
    base: Manifest | None = None
    # What `seed` recorded for the pull that made the configuration in use.
    provenance: jdata.DictJsonData | None = None
    # When an approval last reset the review timers (VER-20).
    last_reviewed: datetime.datetime | None = None
    # When a run last printed the review notice.
    last_notice: datetime.datetime | None = None
    # Configurations to remove once no running environment uses them (VER-40).
    removing: list[str] = field(default_factory=list)
    # The finding ids the last review showed, so the next can lead with what's new (VER-41).
    reviewed_findings: list[str] = field(default_factory=list)

    def check(self) -> None:
        """Refuse a link whose state can't be."""
        if self.in_use is not None and self.unlocked is not None:
            raise ValueError("in-use: set while the configuration is unlocked")
        for config_id in self.removing:
            check_id(config_id)
        if self.in_use is not None:
            check_id(self.in_use)
        kept = self.kept()
        if any(config_id in kept for config_id in self.removing):
            raise ValueError("removing: lists a configuration that's in use or unlocked")

    def kept(self) -> set[str]:
        """The configurations that can't be removed: the one in use, and the one unlocked from."""
        kept = {self.in_use} if self.in_use is not None else set()
        if self.unlocked is not None and self.unlocked.from_id is not None:
            kept.add(self.unlocked.from_id)
        return kept

    def reviewed(self, now: datetime.datetime) -> None:
        """An approval that counts as a review: the timers start again."""
        self.last_reviewed = now
        self.last_notice = None

    def blobs(self) -> set[str]:
        """The blobs its base refers to."""
        return set() if self.base is None else {e.sha256 for e in self.base.files.values()}

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data."""
        return {
            "$schema": LINK_SCHEMA,
            "id": self.id,
            "project-dir": self.project_dir,
            "in-use": self.in_use,
            "unlocked": None if self.unlocked is None else self.unlocked.to_json(),
            "base": None if self.base is None else self.base.to_json(),
            "provenance": self.provenance,
            "last-reviewed": time_text(self.last_reviewed),
            "last-notice": time_text(self.last_notice),
            "removing": list(self.removing),
            "reviewed-findings": list(self.reviewed_findings),
        }

    @staticmethod
    def from_json(data: jdata.JsonData) -> "Link":
        """From JSON data, checking its state."""
        last_reviewed = jdata.find_str(data, "last-reviewed")
        last_notice = jdata.find_str(data, "last-notice")
        link = Link(
            id=jdata.req_str(data, "id"),
            project_dir=jdata.req_str(data, "project-dir"),
            in_use=jdata.find_str(data, "in-use"),
            unlocked=(
                None if jdata.find_dict(data, "unlocked") is None
                else Unlocked.from_json(jdata.req_dict(data, "unlocked"))
            ),
            base=(
                None if jdata.find_dict(data, "base") is None
                else Manifest.from_json(jdata.req_dict(data, "base"))
            ),
            provenance=jdata.find_dict(data, "provenance"),
            last_reviewed=None if last_reviewed is None else parse_time(last_reviewed),
            last_notice=None if last_notice is None else parse_time(last_notice),
            removing=[jdata.req_str(data, *key) for key in jdata.opt_list_keys(data, "removing")],
            reviewed_findings=jdata.find_str_list(data, "reviewed-findings"),
        )
        link.check()
        return link


# ---------------------------------------------------------------------
# In the store


def has_records(store: VerifiedStore, env_id: str) -> bool:
    """Whether the environment has a link or any configuration."""
    return bool(store.list_dir(env_id, CONFIGS_DIR)) or LINK_FILE in store.list_dir(env_id)


def load_link(store: VerifiedStore, project_dir: str, env_id: str) -> Link | None:
    """The environment's link, or None if it has none.

    The index, the directory name and the
    link's own `project-dir` must agree.
    """
    data = store.read_json(env_id, LINK_FILE, schema=LINK_SCHEMA)
    if data is None:
        return None
    where = store.root / env_id / LINK_FILE
    try:
        link = Link.from_json(data)
    except (KeyError, TypeError, ValueError) as err:
        raise VerifiedStoreErr(f"{where}: {err}") from err
    if link.id != env_id or link.project_dir != project_dir:
        raise VerifiedStoreErr(
            f"{where}: is for {link.project_dir} (`{link.id}`), not {project_dir} (`{env_id}`)"
        )
    if store.index().get(project_dir) != env_id:
        raise VerifiedStoreErr(f"{where}: the index doesn't list it for {project_dir}")
    return link


def save_link(store: VerifiedStore, link: Link) -> None:
    """Write the environment's link, and list it in the index.

    The store must be locked.  Every configuration it names must be stored,
    so a link never points at nothing.
    """
    try:
        link.check()
    except ValueError as err:
        raise VerifiedStoreErr(f"{link.id}: {err}") from err
    named = link.kept() | set(link.removing)
    for config_id in sorted(named):
        if load_config(store, link.id, config_id) is None:
            raise VerifiedStoreErr(f"{link.id}: names the configuration `{config_id}`, which isn't stored")
    store.register(link.project_dir, link.id)
    store.write_json(link.id, LINK_FILE, data=link.to_json())


def load_config(store: VerifiedStore, env_id: str, config_id: str) -> LockedConfig | None:
    """The environment's configuration `config_id`, or None if it isn't stored."""
    check_id(config_id)
    data = store.read_json(env_id, CONFIGS_DIR, config_id + CONFIG_SUFFIX, schema=CONFIG_SCHEMA)
    if data is None:
        return None
    where = store.root / env_id / CONFIGS_DIR / (config_id + CONFIG_SUFFIX)
    try:
        config = LockedConfig.from_json(data)
    except (KeyError, TypeError, ValueError) as err:
        raise VerifiedStoreErr(f"{where}: {err}") from err
    if config.id != config_id or config.environment != env_id:
        raise VerifiedStoreErr(f"{where}: is `{config.id}` of `{config.environment}`")
    return config


def list_configs(store: VerifiedStore, env_id: str) -> list[LockedConfig]:
    """The environment's configurations, newest first."""
    found: list[LockedConfig] = []
    for file_name in store.list_dir(env_id, CONFIGS_DIR):
        if not file_name.endswith(CONFIG_SUFFIX):
            raise VerifiedStoreErr(f"{store.root / env_id / CONFIGS_DIR / file_name}: not a configuration")
        config = load_config(store, env_id, file_name.removesuffix(CONFIG_SUFFIX))
        if config is not None:
            found.append(config)
    return sorted(found, key=lambda c: (c.created_at, c.id), reverse=True)


def find_config(store: VerifiedStore, link: Link, name: str) -> LockedConfig | None:
    """The environment's configuration named `name` that isn't waiting for removal."""
    check_name(name)
    return next(
        (c for c in list_configs(store, link.id) if c.name == name and c.id not in link.removing),
        None,
    )


def save_config(store: VerifiedStore, link: Link, config: LockedConfig, replacing: str | None = None) -> None:
    """Store a new configuration for the link's environment.

    The store must be locked, and the snapshot's blobs already in it.  A
    stored configuration is never overwritten.  Its name must be free:
    no other configuration has it, but for `replacing` and those waiting
    for removal.  The caller then points the link at it, and retires the
    one it replaces (`retire`).
    """
    check_id(config.id)
    check_name(config.name)
    if config.environment != link.id:
        raise VerifiedStoreErr(f"{config.id}: is for `{config.environment}`, not `{link.id}`")
    if load_config(store, link.id, config.id) is not None:
        raise VerifiedStoreErr(f"{link.id}: already has a configuration `{config.id}`")
    taken = find_config(store, link, config.name)
    if taken is not None and taken.id != replacing:
        raise VerifiedStoreErr(f"{link.id}: already has a configuration named {config.name!a}")
    for entry in config.snapshot.files.values():
        # Missing or changed blobs are refused now, not at the next run.
        store.get_blob(entry.sha256, entry.size)
    store.write_json(link.id, CONFIGS_DIR, config.id + CONFIG_SUFFIX, data=config.to_json())


def previous_name(name: str) -> str:
    """The name a configuration a pull replaces is kept under: `NAME.previous`."""
    return name + PREVIOUS_SUFFIX


def rename(store: VerifiedStore, link: Link, config_id: str, name: str) -> LockedConfig:
    """Give the configuration `config_id` the name `name`; it, renamed.

    The store must be locked.  Only the name changes: the id, the snapshot
    and the host file stay, so whatever refers to it by id still does.  The
    name must be free, as for `save_config`.
    """
    check_name(name)
    config = load_config(store, link.id, config_id)
    if config is None:
        raise VerifiedStoreErr(f"{link.id}: has no configuration `{config_id}`")
    taken = find_config(store, link, name)
    if taken is not None and taken.id != config_id:
        raise VerifiedStoreErr(f"{link.id}: already has a configuration named {name!a}")
    renamed = replace(config, name=name)
    store.write_json(link.id, CONFIGS_DIR, config_id + CONFIG_SUFFIX, data=renamed.to_json())
    return renamed


def keep_previous(store: VerifiedStore, link: Link, name: str) -> str | None:
    """Make room for a new configuration named `name`, as a pull does; the one to retire, if any.

    The store must be locked.  The configuration named `name` becomes
    `NAME.previous`, so going back is `scoter switch NAME.previous`.  The one
    that had that name loses it (it's no longer found by name) and is
    returned: the caller retires it (`retire`) once the link no longer
    names it, so a name keeps one step of history.
    """
    current = find_config(store, link, name)
    if current is None:
        return None
    older = find_config(store, link, previous_name(name))
    if older is not None and older.id not in link.removing:
        # Waiting for removal, it has no name a lookup finds.
        link.removing.append(older.id)
    rename(store, link, current.id, previous_name(name))
    return None if older is None else older.id


def retire(store: VerifiedStore, link: Link, config_id: str, running: Collection[str]) -> bool:
    """Remove a configuration that's been replaced; whether it went now (VER-40).

    The store must be locked.  One a running environment uses
    (`running`, by configuration id) is marked for removal instead, in
    the link, which the caller saves; `remove_retired` removes it later.
    The configuration in use, or unlocked from, is refused.
    """
    check_id(config_id)
    if config_id in link.kept():
        raise VerifiedStoreErr(f"{link.id}: `{config_id}` is still in use")
    if config_id in running:
        if config_id not in link.removing:
            link.removing.append(config_id)
        return False
    store.remove(link.id, CONFIGS_DIR, config_id + CONFIG_SUFFIX)
    if config_id in link.removing:
        link.removing.remove(config_id)
    return True


def remove_retired(store: VerifiedStore, link: Link, running: Collection[str]) -> list[str]:
    """Remove the configurations marked for removal that no running environment uses; those removed.

    The store must be locked.  The caller saves the link afterwards; a
    crash between leaves the link marking a configuration that's already
    gone, which the next call drops.
    """
    removed: list[str] = []
    for config_id in list(link.removing):
        if retire(store, link, config_id, running):
            removed.append(config_id)
    return removed


def used_blobs(store: VerifiedStore) -> set[str]:
    """The blobs every environment's configurations and base refer to."""
    used: set[str] = set()
    for project_dir, env_id in store.index().items():
        link = load_link(store, project_dir, env_id)
        if link is not None:
            used |= link.blobs()
        for config in list_configs(store, env_id):
            used |= config.blobs()
    return used


def remove_unused_blobs(store: VerifiedStore) -> int:
    """Remove the blobs no configuration or base refers to; how many went.  The store must be locked."""
    return store.remove_unused_blobs(used_blobs(store))


def forget(store: VerifiedStore, project_dir: str, env_id: str) -> None:
    """Remove the environment: its link, its configurations, and its place in the index.

    The store must be locked.  Out of the index first, so it never names a
    missing environment.  The caller removes the blobs nothing uses any more.
    """
    store.unregister(project_dir)
    store.remove(env_id)


def remove_link(store: VerifiedStore, link: Link) -> None:
    """Remove the environment's link, leaving no configuration in use; and the
    environment too, when it has no configurations left.  The store must be locked."""
    store.remove(link.id, LINK_FILE)
    if not has_records(store, link.id):
        forget(store, link.project_dir, link.id)
