#SPDX:MIT
"""List and remove the user's locked configurations.

`main_list.py` lists every environment in the store, with its
configurations, and `main_remove.py` removes them.  A configuration is
chosen by its id, as `scoter list` shows it, for any project; or,
for the current project, by its name, or all of them.

* Removing the configuration in use leaves the project with none: its next
  run asks for `scoter pull`.
* One a running environment uses is marked, and goes once it stops
  (VER-40).
* The one an unlocked copy was unlocked from is refused: `scoter lock` or
  `scoter lock --revert` first.
* An environment left with no configurations, and nothing unlocked, is
  forgotten: its link goes, and it leaves the store's index.

The blobs nothing refers to any more go too.
"""
import datetime
import enum
import os
from collections.abc import Collection, Iterable
from dataclasses import dataclass

from ..err import VerifiedStoreErr
from ..user_config import UserConfig
from . import configs, store


class Pick(enum.Enum):
    """Which of the project's configurations `remove` takes."""

    # Those the names choose.
    NAMED = "named"
    # All of them (`--all`), and the environment with them.
    ALL = "all"


@dataclass(frozen=True)
class Environment:
    """A project's place in the verified store."""

    store: store.VerifiedStore
    project_dir: str
    env_id: str

    def link(self) -> configs.Link | None:
        """The environment's link; None without one."""
        return configs.load_link(self.store, self.project_dir, self.env_id)

    def configs(self) -> list[configs.LockedConfig]:
        """Its configurations, newest first."""
        return configs.list_configs(self.store, self.env_id)


def open_store(config: UserConfig) -> store.VerifiedStore | None:
    """The user's verified store; None when there isn't one yet."""
    verified = config.optional_path("verification", "store")
    if verified is None or not verified.is_dir():
        return None
    return store.VerifiedStore.open(verified)


def environment(config: UserConfig, verified_store: store.VerifiedStore) -> Environment | None:
    """The current project's environment in the store; None without a project."""
    found = config.found_project()
    if found is None or found.found_by == "start":
        return None
    project_dir = os.path.realpath(found.project_dir)
    return Environment(verified_store, project_dir, store.project_id(config, project_dir))


def environments(verified_store: store.VerifiedStore) -> list[Environment]:
    """Every environment in the store's index, by project directory."""
    return [
        Environment(verified_store, project_dir, env_id)
        for project_dir, env_id in sorted(verified_store.index().items())
    ]


def describe(config: configs.LockedConfig, link: configs.Link | None) -> str:
    """One line for the configuration: its id, name and age, and whether it's in use."""
    marks = []
    if link is not None:
        if config.id == link.in_use:
            marks.append("in use")
        if link.unlocked is not None and config.id == link.unlocked.from_id:
            marks.append("unlocked")
        if config.id in link.removing:
            marks.append("removed, waiting for its run to stop")
    created = config.created_at.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"{config.id}  {config.name}  made {created}" + (f"  ({', '.join(marks)})" if marks else "")


def select(env: Environment, names: Iterable[str]) -> tuple[list[configs.LockedConfig], list[str]]:
    """The project's configurations `names` choose, by name or id; and the names that chose none."""
    link = env.link()
    removing = set(link.removing) if link is not None else set()
    known = [c for c in env.configs() if c.id not in removing]
    chosen: list[configs.LockedConfig] = []
    unmatched: list[str] = []
    for name in names:
        found = [c for c in known if name in (c.id, c.name)]
        if found:
            chosen.extend(c for c in found if c not in chosen)
        else:
            unmatched.append(name)
    return chosen, unmatched


def find_ids(verified_store: store.VerifiedStore, ids: Iterable[str]) -> dict[Environment, list[configs.LockedConfig]]:
    """The configurations with these ids, in any project, by environment."""
    wanted = set(ids)
    found: dict[Environment, list[configs.LockedConfig]] = {}
    for env in environments(verified_store):
        matched = [c for c in env.configs() if c.id in wanted]
        if matched:
            found[env] = matched
    return found


def remove(env: Environment, ids: Iterable[str], running: Collection[str], *, everything: bool = False) -> list[str]:
    """Remove the configurations `ids` (with `everything`, all the project's); the ids removed or marked.

    Under the store's lock.  The one in use leaves the project with none in
    use; one a running environment uses is marked rather than removed; the
    one an unlocked copy came from is refused.  An environment left with
    nothing is forgotten.
    """
    wanted = set(ids)
    with env.store.locked():
        link = env.link()
        if everything:
            wanted |= {c.id for c in env.configs()}
        if not wanted:
            return []
        if link is None:
            # Configurations without a link: only a crash leaves them.
            link = configs.Link(env.env_id, env.project_dir)
        if link.unlocked is not None and (everything or link.unlocked.from_id in wanted):
            raise VerifiedStoreErr(
                f"{env.project_dir}: its configuration is unlocked; `scoter lock` or "
                "`scoter lock --revert` first"
            )
        if link.in_use in wanted:
            link.in_use = None
        done: list[str] = []
        for config_id in sorted(wanted):
            if configs.load_config(env.store, env.env_id, config_id) is None:
                continue
            configs.retire(env.store, link, config_id, running)
            done.append(config_id)
        if link.unlocked is None and not configs.list_configs(env.store, env.env_id):
            configs.remove_link(env.store, link)
        else:
            configs.save_link(env.store, link)
        configs.remove_unused_blobs(env.store)
    return done
