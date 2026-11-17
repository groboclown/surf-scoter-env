#SPDX:MIT
"""`scoter pull approve`: store a working directory as a locked configuration.

Approval never reads a report.  It takes a working directory's id and the
id of the analysis the reviewer last showed, and:

* snapshots the working directory again, and refuses unless its files and
  host file are exactly what that analysis named (VER-28);
* checks it again, and refuses while any check refuses;
* resolves the host file again: the developer's own entries are kept, the
  rest found again through the path checks, and any difference is shown;
* under the store's lock (VER-35), stores the build's files and the host
  file as a new locked configuration, keeps the one it replaces as
  `NAME.previous` (retiring the older one), and points the link at the new
  one, with the base, the provenance and the review timers reset.
"""
import os
from dataclasses import dataclass

from .. import host_file, jdata
from ..user_config import UserConfig
from ..verified import configs
from ..verified.store import now_utc
from . import analyze, seed, workdir


@dataclass(frozen=True)
class Approved:
    """The configuration an approval stored."""

    config_id: str
    name: str
    # The name the configuration it replaced is kept under, if there was one.
    previous: str | None
    # How the host file differs from the working directory's.
    host_changes: list[str]

    def to_json(self) -> jdata.DictJsonData:
        """As JSON data, as `scoter pull approve` prints it."""
        return {
            "configuration": self.config_id,
            "name": self.name,
            "previous": self.previous,
            "host-changes": list(self.host_changes),
        }


def approve(
    config: UserConfig,
    workdir_id: str,
    analysis_id: str,
    *,
    generate: seed.Generate,
    name: str | None = None,
    path_env: str | None = None,
    running: frozenset[str] = frozenset(),
) -> Approved:
    """Approve the working directory as the analysis `analysis_id` showed it.

    What happens to the configuration it replaces depends on where it was
    seeded from:

    * the project (`scoter pull`): it's kept as `NAME.previous`, and the base,
      the provenance and the review timers are reset;
    * the unlocked copy (`scoter lock`): the configuration unlocked from is
      retired, and the timers carry on, as a lock shows only what changed;
    * the configuration in use (`scoter review`): it's retired, and the
      timers reset.

    The new one takes `name`, or the name of the one it replaces, or
    `default`.  `running` are the configurations running environments use,
    which a retired configuration waits for (VER-40).
    """
    if name is not None:
        configs.check_name(name)
    wd = workdir.open_workdir(config, workdir_id)
    record = wd.analysis(analysis_id)
    inspection = analyze.inspect(config, wd, generate=generate, path_env=path_env)
    if inspection.files.manifest.id != record.files.id or inspection.host_hash != record.host_file:
        raise workdir.PullErr(
            f"The working directory changed since analysis {analysis_id}: analyze it again, "
            "and review what changed."
        )
    refused = inspection.report.refused
    if refused:
        raise workdir.PullErr(
            "The configuration can't be approved; the checks refuse it:\n"
            + "\n".join(f"  {f.key}: {f.message}" for f in refused)
        )
    resolution = host_file.resolve(inspection.staged, path_env, inspection.host_contents)
    if resolution.report.refused:
        raise workdir.PullErr(
            "The host commands can't be approved:\n"
            + "\n".join(f"  {f.key}: {f.message}" for f in resolution.report.refused)
        )
    provenance = wd.provenance
    kind = jdata.find_str(provenance.source, "kind")
    verified = seed.open_store(config)
    now = now_utc()
    finding_ids = analyze.finding_ids(inspection.report.findings, inspection.staged, inspection.resolved)
    with verified.locked():
        link = configs.load_link(verified, provenance.project_dir, provenance.env_id)
        if link is None:
            link = configs.Link(provenance.env_id, os.path.realpath(provenance.project_dir))
        replaced = _replaced(link, kind, provenance.source)
        old = None if replaced is None else configs.load_config(verified, link.id, replaced)
        name = name or (old.name if old is not None else configs.DEFAULT_NAME)
        configs.check_name(configs.previous_name(name))
        inspection.build.store(verified)
        if kind == seed.FROM_PROJECT:
            # The base's blobs, stored at seeding, must still be there to record it.
            for entry in provenance.seeded.files.values():
                verified.get_blob(entry.sha256, entry.size)
        new = configs.LockedConfig(
            configs.new_config_id(now), name, link.id, now, inspection.build.manifest, resolution.contents,
        )
        link.in_use = new.id
        link.unlocked = None
        previous = None
        if kind == seed.FROM_PROJECT:
            previous = configs.find_config(verified, link, name)
            configs.keep_previous(verified, link, name)
            configs.save_config(verified, link, new)
            # The base is what was last pulled from the project.
            link.base = provenance.seeded
            link.provenance = provenance.to_json()
        else:
            configs.save_config(verified, link, new, replacing=replaced)
        if kind != seed.FROM_UNLOCKED:
            # Only a pull or a review shows everything, and resets the timers (VER-20).
            link.reviewed(now)
            link.reviewed_findings = finding_ids
        configs.save_link(verified, link)
        # Once the link no longer names them.
        if replaced is not None and old is not None:
            configs.retire(verified, link, replaced, running)
        configs.remove_retired(verified, link, running)
        configs.save_link(verified, link)
    return Approved(
        new.id, name, None if previous is None else configs.previous_name(name), resolution.changes,
    )


def _replaced(link: configs.Link, kind: str | None, source: jdata.DictJsonData) -> str | None:
    """The configuration an approval from `kind` replaces, after checking the link allows it."""
    if kind == seed.FROM_PROJECT:
        if link.unlocked is not None:
            raise workdir.PullErr(
                "Your configuration is unlocked: run `scoter lock` (or `scoter lock --revert`) first, "
                "so a pull never drops your edits."
            )
        return None
    if kind == seed.FROM_UNLOCKED:
        if link.unlocked is None:
            raise workdir.PullErr("Your configuration isn't unlocked any more.")
        return link.unlocked.from_id
    if kind == seed.FROM_LOCKED:
        reviewed = jdata.find_str(source, "config")
        if link.in_use is None or link.in_use != reviewed:
            raise workdir.PullErr(
                "The configuration in use changed while it was reviewed; review it again."
            )
        return reviewed
    raise workdir.PullErr(f"the working directory's source {kind!a} isn't one approval knows")
