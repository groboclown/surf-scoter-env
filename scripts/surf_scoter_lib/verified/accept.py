#SPDX:MIT
"""Store the user's acceptance of a project's review.

`main_verify.py` calls this when the user uses a review file that parsed
and passed the checks.  See docs/contributors/projects-v1.md, steps 5 and 15.

An acceptance keeps, in the verified store:

* `accepted.json`: the accepted settings the generator gave from the review
  (the SUC's `review: accepted`), which later runs give back to it;
* `review.yaml`: the review file as the user left it;
* `suggestion.json`: the project file and dev container it was reviewed from,
  and the suggestion as the review wrote it (`review`), which the next
  review compares with (step 17);
* `containers.json`: the build's `verification: verified-containers` containers, each
  directory's manifest (their contents as blobs), for the run to build from
  (step 18);

and, in the environment's record, the snapshot of the project's files (its
contents as blobs) and the setup values it was reviewed under.
"""
import datetime
import json
import os

from .. import jdata
from ..err import VerifiedStoreErr
from ..user_config import UserConfig
from . import lookup, records, snapshot, store

REVIEW_FILE = "review.yaml"
SUGGESTION_FILE = "suggestion.json"
# The `verification: verified-containers` containers the build uses, as accepted: each
# directory's manifest, their contents in the store, for the run to build from.
CONTAINERS_FILE = "containers.json"
CONTAINERS_SCHEMA = "surf-scoter-accepted-containers-1"


def store_acceptance(
    config: UserConfig,
    taken: snapshot.Snapshot,
    review_text: bytes,
    accepted: jdata.DictJsonData,
    branch: str | None,
    now: datetime.datetime,
    *,
    suggested: jdata.DictJsonData | None = None,
) -> records.Acceptance:
    """Record the user's acceptance of `accepted`, for the project's files in `taken`.

    `config` is the universal configuration file the review was made from;
    `taken` the snapshot of the project's files taken before the review was
    written, so the acceptance is for the files the user saw.  `suggested`
    is the suggestion as the review wrote it (`review.suggested`), which the
    next review compares with (step 17).
    """
    project = config.active_location("project-dir")
    verified = config.optional_path("verification", "store")
    if project is None:
        raise VerifiedStoreErr("there's no project to accept")
    if verified is None:
        raise VerifiedStoreErr("the setup gives no `verification: paths: store` to keep it in")
    project_dir = os.path.realpath(project)
    containers = lookup.verified_container_snapshots(config)
    acceptance = records.Acceptance(
        id=records.new_acceptance_id(now),
        kind=records.Kind.REVIEWED,
        accepted_at=now,
        expires_at=records.parse_time(jdata.req_str(accepted, "expires-at")),
        setup=records.SetupRecord(records.setup_values(
            config, lookup.vcs_section(config), {d: c.manifest.id for d, c in containers.items()},
        )),
        suggestion=taken.manifest,
        branch=branch,
    )
    suggestion: jdata.DictJsonData = {
        "project": config.project_setup(),
        "dev-container": config.dev_container(),
        "review": suggested,
    }
    verified_store = store.VerifiedStore.open(verified, create=True)
    env_id = store.project_id(config, project_dir)
    with verified_store.locked():
        record = records.load_environment(verified_store, project_dir, env_id)
        if record is None:
            record = records.EnvironmentRecord(env_id, project_dir, [])
        taken.store(verified_store)
        where = (env_id, records.ACCEPTANCES_DIR, acceptance.id)
        verified_store.write_bytes(*where, lookup.ACCEPTED_FILE, data=_json(accepted))
        verified_store.write_bytes(*where, REVIEW_FILE, data=review_text)
        verified_store.write_bytes(*where, SUGGESTION_FILE, data=_json(suggestion))
        for container in containers.values():
            container.store(verified_store)
        verified_store.write_json(*where, CONTAINERS_FILE, data={
            "$schema": CONTAINERS_SCHEMA,
            "containers": {d: c.manifest.to_json() for d, c in sorted(containers.items())},
        })
        record.add(acceptance)
        records.save_environment(verified_store, record)
    return acceptance


def _json(data: jdata.JsonData) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
