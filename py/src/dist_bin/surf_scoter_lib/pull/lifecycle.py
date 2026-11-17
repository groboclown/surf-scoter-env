#SPDX:MIT
"""`scoter unlock`, `lock`, `switch`, `review` and `init`: moving a configuration between its states.

* **Locked**, the normal state: in the store, run from a staged copy.
* **Unlocked**: `${pull:paths:unlocked-dir}/ENV-ID`, for the developer to
  edit and run; `scoter lock` reviews it and stores it again, or `scoter lock
  --revert` throws the edits away.

Locking and unlocking repoint the environment's link, as `scoter switch` does;
only these commands, and approval, change it.  `scoter review` looks at the
configuration in use again, leading with what's new (VER-41).  `scoter init`
starts a configuration from a distribution template, for a project with no
files of its own; it never writes into the project.
"""
import datetime
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import fileutil, host_file, review, run_config
from ..user_config import FoundProject, UserConfig
from ..verified import configs, lookup, snapshot, store
from ..verified.store import now_utc
from . import porcelain, seed, unlocked, workdir

DEFAULT_TEMPLATE = "default"


@dataclass(frozen=True)
class Context:
    """The project, its store and its link, as the commands find them."""

    found: FoundProject
    verified: store.VerifiedStore
    project_dir: str
    env_id: str

    def link(self) -> configs.Link | None:
        """The environment's link, read now."""
        return configs.load_link(self.verified, self.project_dir, self.env_id)


def context(config: UserConfig, *, start_ok: bool = False) -> Context:
    """The project the command is for, and its store."""
    found = seed.require_found(config, start_ok=start_ok)
    project_dir = os.path.realpath(found.project_dir)
    return Context(found, seed.open_store(config), project_dir, store.project_id(config, project_dir))


def template_dir(config: UserConfig, found: FoundProject, name: str) -> Path:
    """The distribution's template `name`; never one inside the project, its clone or its repository (VER-7)."""
    configs.check_name(name)
    base = config.optional_path("pull", "templates-dir")
    if base is None:
        raise unlocked.UnlockedErr("The setup names no `pull: paths: templates-dir`.")
    unlocked.check_location(config, base, "`pull: paths: templates-dir`", found=found)
    directory = base / name
    if not directory.is_dir():
        known = sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
        raise unlocked.UnlockedErr(
            f"There's no template {name!a}" + (f"; there are: {', '.join(known)}" if known else "")
        )
    return directory


# ---------------------------------------------------------------------
# unlock


def unlock(
    config: UserConfig, *, template: str | None = None, replace: bool = False,
    now: datetime.datetime | None = None,
) -> Path:
    """Copy the configuration in use, or a template, into the unlocked directory; where it went.

    It refuses when the configuration is already unlocked, and when the
    unlocked directory already holds files (VER-36), unless `replace`, which
    first renames them to a backup beside it.  From a template, the base
    becomes the project's files as they are now, by hash, so a run's notice
    reports a later change to them, not the files the developer chose not
    to use.
    """
    now = now or now_utc()
    ctx = context(config, start_ok=template is not None)
    files_dir, file_name = seed.files_location(config, ctx.found)
    limits = lookup.limits(config)
    root = unlocked.root(config, ctx.env_id)
    source_dir = None if template is None else template_dir(config, ctx.found, template)
    with ctx.verified.locked():
        link = ctx.link()
        if link is not None and link.unlocked is not None:
            raise unlocked.UnlockedErr(f"Your configuration is already unlocked, in {root}.")
        if template is None and (link is None or link.in_use is None):
            raise unlocked.UnlockedErr(
                "There's no configuration to unlock: `scoter pull` makes one from the project's "
                "files, or `scoter unlock --from-template NAME` starts one."
            )
        if unlocked.holds_files(root):
            if not replace:
                raise unlocked.UnlockedErr(
                    f"{root} already holds files, perhaps edits left from before: move or remove "
                    "them, or give --replace to keep them as a backup beside it."
                )
            backup = root.with_name(f"{root.name}.backup-{now.strftime('%Y%m%dT%H%M%SZ')}")
            os.rename(root, backup)
        if source_dir is not None:
            taken = snapshot.snapshot_source(source_dir, file_name, limits)
            host = None
        else:
            assert link is not None and link.in_use is not None  # nosec: B101  # checked above
            in_use = configs.load_config(ctx.verified, link.id, link.in_use)
            if in_use is None:
                raise unlocked.UnlockedErr(f"Your configuration in use ({link.in_use}) isn't in the store.")
            taken = snapshot.from_store(ctx.verified, in_use.snapshot)
            host = in_use.host_file
        fileutil.private_dir(root, [unlocked.FILES_DIR])
        snapshot.write_files(taken.manifest, root / unlocked.FILES_DIR, limits, lambda rel, _: taken.contents[rel])
        if host is not None:
            host_file.write(root, host)
        link = link or configs.Link(ctx.env_id, ctx.project_dir)
        link.unlocked = configs.Unlocked(now, link.in_use, template)
        link.in_use = None
        if template is not None:
            live = (
                snapshot.snapshot_source(files_dir, file_name, limits).manifest if files_dir.is_dir()
                else snapshot.Manifest({})
            )
            link.provenance = {
                "source": {"kind": "template", "name": template, "snapshot": taken.manifest.id},
                "source-id": live.id,
            }
        configs.save_link(ctx.verified, link)
    return root


# ---------------------------------------------------------------------
# lock


def check_keep(config: UserConfig, ctx: Context, keep: Path) -> None:
    """Refuse a `--keep DIR` that exists, or where files could be taken for configuration (VER-38)."""
    if os.path.lexists(keep):
        raise unlocked.UnlockedErr(f"{keep} already exists; --keep names a new directory")
    also: dict[str, Path] = {}
    for section, key, name in (
        ("scoter", "user-dir", "scoter:user-dir"), ("pull", "unlocked-dir", "pull:unlocked-dir"),
    ):
        path = config.optional_path(section, key)
        if path is not None:
            also[name] = path
    unlocked.check_location(config, keep, "--keep", found=ctx.found, also=also)


def _dispose(root: Path, keep: Path | None, say: Callable[[str], None]) -> None:
    """Remove the unlocked copy, or rename it to `keep`; a failure is said, never raised (VER-38)."""
    if keep is not None:
        try:
            os.rename(root, keep)
            say(f"Your unlocked copy is now {keep}; it's yours, and nothing tracks it.")
        except OSError as err:
            say(f"Couldn't move {root} to {keep} ({review.clean(str(err))}); it's still there, unused.")
        return
    if not fileutil.rm(root) and os.path.lexists(root):
        say(f"Couldn't remove all of {root}; it's unused, and `scoter unlock` refuses until it's gone.")


def lock(
    config: UserConfig,
    options: porcelain.Options,
    *,
    keep: Path | None = None,
    say: Callable[[str], None] = print,
    review_with: Callable[[UserConfig, str, Path], int] = porcelain.run_reviewer,
) -> int:
    """Review the unlocked copy, store it, and remove (or keep) the copy; 0 when it was approved."""
    ctx = context(config)
    link = ctx.link()
    if link is None or link.unlocked is None:
        say("Your configuration isn't unlocked: nothing to lock.")
        return 1
    root = unlocked.root(config, ctx.env_id)
    if keep is not None:
        check_keep(config, ctx, keep)
    options.source = seed.FROM_UNLOCKED
    code = porcelain.pull(config, options, say=say, review_with=review_with)
    if code != 0:
        say("Your configuration is still unlocked.")
        return code
    _dispose(root, keep, say)
    return 0


def revert(
    config: UserConfig,
    *,
    keep: Path | None = None,
    yes: bool = False,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> int:
    """Throw the edits away, and go back to the configuration unlocked from; 0 when it did."""
    ctx = context(config)
    _, file_name = seed.files_location(config, ctx.found)
    link = ctx.link()
    if link is None or link.unlocked is None:
        say("Your configuration isn't unlocked: nothing to revert.")
        return 1
    root = unlocked.root(config, ctx.env_id)
    if keep is not None:
        check_keep(config, ctx, keep)
    from_id = link.unlocked.from_id
    before = None if from_id is None else configs.load_config(ctx.verified, link.id, from_id)
    if from_id is not None and before is None:
        say(
            f"The configuration you unlocked ({from_id}) isn't in the store any more: `scoter lock` "
            "your edits, or `scoter pull` again."
        )
        return 1
    try:
        now_files, _ = unlocked.read(root, file_name, lookup.limits(config))
        changed = (
            seed.differences(before.snapshot, now_files.manifest) if before is not None
            else sorted(now_files.manifest.files)
        )
    except (OSError, unlocked.UnlockedErr, snapshot.SnapshotErr) as err:
        say(f"Your unlocked copy can't be compared ({review.clean(str(err))}).")
        changed = ["(everything in it)"]
    if changed and not yes:
        say("These files differ from the configuration you unlocked, and will be thrown away:")
        for path in changed:
            say(f"  {review.clean(path)}")
        try:
            answer = ask("Throw them away?  Type `yes`; Enter keeps them: ")
        except EOFError:
            answer = ""
        if answer.strip() != "yes":
            say("Kept: your configuration is still unlocked.")
            return 1
    with ctx.verified.locked():
        link = ctx.link()
        if link is None or link.unlocked is None or link.unlocked.from_id != from_id:
            say("Your configuration changed meanwhile; nothing was reverted.")
            return 1
        if from_id is None:
            # Started from a template, with nothing before it: no configuration.
            configs.remove_link(ctx.verified, link)
        else:
            # The review timers carry on: an unlock and a revert are neither a review nor a change.
            link.unlocked = None
            link.in_use = from_id
            configs.save_link(ctx.verified, link)
    _dispose(root, keep, say)
    say("Reverted." if from_id is not None else "Reverted: the project has no configuration now.")
    return 0


# ---------------------------------------------------------------------
# switch and review


def switch(
    config: UserConfig, name: str, *, generate: seed.Generate, path_env: str | None = None,
) -> configs.LockedConfig:
    """Switch the environment to its configuration `name`, after the checks a run makes."""
    ctx = context(config)
    link = ctx.link()
    if link is None:
        raise unlocked.UnlockedErr("This project has no configurations: `scoter pull` makes one.")
    if link.unlocked is not None:
        raise unlocked.UnlockedErr("Your configuration is unlocked: `scoter lock` or `scoter lock --revert` first.")
    chosen = configs.find_config(ctx.verified, link, name)
    if chosen is None:
        names = sorted(c.name for c in configs.list_configs(ctx.verified, ctx.env_id) if c.id not in link.removing)
        raise unlocked.UnlockedErr(f"There's no configuration {name!a}; there are: {', '.join(names) or 'none'}")
    with tempfile.TemporaryDirectory(dir=workdir.pull_root(config)) as tmp:
        run_config.check_config(config, ctx.verified, chosen, Path(tmp), generate=generate, path_env=path_env)
    with ctx.verified.locked():
        link = ctx.link()
        assert link is not None  # nosec: B101  # read above, and only removed by revert
        if link.unlocked is not None:
            raise unlocked.UnlockedErr("Your configuration was unlocked meanwhile.")
        link.in_use = chosen.id
        configs.save_link(ctx.verified, link)
    return chosen


def review_in_use(
    config: UserConfig,
    options: porcelain.Options,
    *,
    say: Callable[[str], None] = print,
    review_with: Callable[[UserConfig, str, Path], int] = porcelain.run_reviewer,
) -> int:
    """Review the configuration in use again; 0 when it was approved (VER-20, VER-41)."""
    ctx = context(config)
    link = ctx.link()
    if link is None or link.in_use is None:
        say(
            "An unlocked configuration is reviewed by `scoter lock`."
            if link is not None and link.unlocked is not None
            else "This project has no configuration to review: `scoter pull` makes one."
        )
        return 1
    options.source = seed.FROM_LOCKED
    options.known_findings = list(link.reviewed_findings)
    return porcelain.pull(config, options, say=say, review_with=review_with)


# ---------------------------------------------------------------------
# init


def init(config: UserConfig, name: str | None = None, *, say: Callable[[str], None] = print) -> int:
    """Start a configuration from a template, for a project with no files of its own."""
    ctx = context(config, start_ok=True)
    files_name = config.optional_value("scoter", "project-files-dir")
    if files_name is None:
        say("The setup names no `scoter: values: project-files-dir`.")
        return 1
    files_dir = Path(ctx.project_dir) / files_name
    if os.path.lexists(files_dir):
        say(
            f"This project already has {files_dir}.  `scoter pull` reviews its files and makes them "
            "your configuration; `scoter unlock --from-template NAME` replaces them with a template, "
            "if that's what you mean."
        )
        return 1
    template = name or config.optional_value("pull", "default-template") or DEFAULT_TEMPLATE
    root = unlock(config, template=template)
    files = root / unlocked.FILES_DIR
    say(f"Started your configuration from the `{template}` template, in {files}.")
    say("Next:")
    say(f"  * Edit the files in {files}, and try them with `scoter run`; it warns that they're unreviewed.")
    say("  * `scoter lock` reviews them, and stores them as your configuration.")
    say("  * `scoter push` adds them to the project, if it's yours.")
    say("  * `scoter lock --revert` gives up, and leaves the project as it was.")
    return 0
