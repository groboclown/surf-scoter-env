#SPDX:MIT
"""The earlier acceptance a new review compares with (step 17).

`main_verify.py` uses this to find what the project suggested last time,
and what the user accepted, so the review can show what changed since,
separately from what the user changed.  The user's own version comes from
the acceptance's `review.yaml`, which only the generator can read
(`gen-scoter-uc --previous-review`); this module finds the file.
"""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .. import jdata
from ..user_config import UserConfig
from . import accept, lookup, records, snapshot, store

# The key in `suggestion.json` holding the suggestion as the review wrote it.
REVIEW_VIEW = "review"


@dataclass
class Previous:
    """An earlier acceptance of the project, and what it was made from."""

    acceptance: records.Acceptance
    # The project's suggestion as that review wrote it (`review.suggested`).
    suggested: jdata.DictJsonData
    # The project file as it was then (`suggestion.json`'s `project`), with
    # its `${...}` as written: whether a value changed in the project or in
    # how the setup resolves it.
    project: jdata.DictJsonData | None
    # The acceptance's `review.yaml`, for the generator to read.
    review_file: Path
    # Its snapshot's files, read from the store.
    files: dict[str, bytes] = field(default_factory=dict)
    # The setup values that changed since: `host-command-checks: verification:shells`, say.
    setup_changes: list[str] = field(default_factory=list)
    # What the user accepted, as the generator read their `review.yaml`
    # (`previous-review: data`); filled in by the caller.
    accepted: jdata.DictJsonData | None = None


def find(config: UserConfig, branch: str | None) -> Previous | None:
    """The project's newest acceptance on `branch`, or else its newest; None without one.

    Expired acceptances count: an expiry is the commonest reason to review
    again.  A blanket acceptance has no snapshot, and one made before
    reviews stored their suggestion has nothing to compare with; both are
    passed over.
    """
    project = config.active_location("project-dir")
    verified = config.optional_path("verification", "store")
    if project is None or verified is None or not verified.is_dir():
        return None
    project_dir = os.path.realpath(project)
    verified_store = store.VerifiedStore.open(verified)
    env_id = store.project_id(config, project_dir)
    record = records.load_environment(verified_store, project_dir, env_id)
    if record is None:
        return None
    candidates = [a for a in record.acceptances if a.suggestion is not None]
    on_branch = [a for a in candidates if branch is not None and a.branch == branch]
    for acceptance in on_branch or candidates:
        where = (env_id, records.ACCEPTANCES_DIR, acceptance.id)
        data = verified_store.read_bytes(*where, accept.SUGGESTION_FILE, max_size=store.MAX_JSON_SIZE)
        stored = json.loads(data) if data else None
        suggested = jdata.find_dict(stored, REVIEW_VIEW) if stored else None
        if suggested is None:
            continue
        manifest = acceptance.suggestion
        assert manifest is not None  # nosec: B101  # chosen for having one
        return Previous(
            acceptance=acceptance,
            suggested=suggested,
            project=jdata.find_dict(stored, "project"),
            review_file=verified_store.root.joinpath(*where, accept.REVIEW_FILE),
            files=_files(verified_store, manifest, lookup.limits(config)),
            setup_changes=records.setup_changes(acceptance.setup.values, records.setup_values(
                config, lookup.vcs_section(config), lookup.verified_containers(config),
            )),
        )
    return None


def _files(
    verified_store: store.VerifiedStore, manifest: snapshot.Manifest, limits: snapshot.SnapshotLimits,
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for rel, entry in manifest.files.items():
        if entry.size <= limits.max_file_size:
            files[rel] = verified_store.get_blob(entry.sha256, entry.size)
    return files
