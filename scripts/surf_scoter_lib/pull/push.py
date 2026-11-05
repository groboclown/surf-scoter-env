#SPDX:MIT
"""`scoter push`: offer the configuration in use back to the project.

Push reads a locked configuration from the store, never the unlocked copy,
so what it writes has been reviewed.  It never writes the host file: only
the configuration's `files/` tree (VER-16, VER-42).

* **The patch** (VER-15): the configuration against the project's
  `.surf-scoter.d` as it is on disk, read with the snapshot rules, in `git
  diff` format, shown through `review.visible`.  Files the project has and
  the configuration doesn't are listed, never deleted.  Uncommitted changes
  the push would overwrite are marked, and so are lines naming this
  computer: the home directory, the host name, and files the setup's
  `project-files` checks flag (VER-16's backstop, printed even with `--yes`).
* **The write** (VER-15): relative to a directory descriptor, with
  `O_NOFOLLOW` on each part of the path, replacing only regular files; each
  file is checked, just before it's replaced, against the hash the patch
  was made from, and refused if it changed.
* **The first push** (VER-39): with no `.surf-scoter.d` at all, the files
  are written into a new directory beside it, which is renamed into place
  only if nothing appeared there meanwhile.
* **The base**: the configuration pushed becomes the link's base, so the
  run's notice (VER-21) doesn't report the developer's own push; files the
  push didn't write still differ from it, so they're still reported.
"""
import difflib
import hashlib
import os
import socket
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from .. import checks, fileutil, review, vcs_files
from ..user_config import UserConfig
from ..verified import configs, lookup, snapshot
from . import lifecycle, seed, workdir

TEMP_PREFIX = ".surf-scoter-push-"
MODE_DIR = 0o755


class PushErr(workdir.PullErr):
    """The push can't go ahead."""


@dataclass
class Change:
    """One file the push writes, or leaves."""

    path: str
    # `added`, `changed`, `same`, or `extra` (the project's, left in place).
    kind: str
    # The project's file's hash when the patch was made; None when it had none.
    live_sha256: str | None
    entry: snapshot.FileEntry | None
    uncommitted: bool = False


@dataclass
class Plan:
    """What a push would do, and the patch that shows it."""

    project_dir: Path
    target: Path
    config: configs.LockedConfig
    contents: dict[str, bytes]
    changes: list[Change]
    first: bool
    patch: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def writes(self) -> list[Change]:
        """The files the push writes."""
        return [c for c in self.changes if c.kind in ("added", "changed")]

    @property
    def extras(self) -> list[Change]:
        """The project's files the configuration doesn't have: left for the developer."""
        return [c for c in self.changes if c.kind == "extra"]


@dataclass
class Result:
    """What the push wrote, and what it didn't."""

    written: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(data: bytes) -> list[str] | None:
    try:
        return review.file_lines(data.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def plan(config: UserConfig, name: str | None = None) -> Plan:
    """What pushing the configuration in use (or the one named `name`) would do."""
    ctx = lifecycle.context(config)
    files_name = config.optional_value("scoter", "project-files-dir")
    _, file_name = seed.files_location(config, ctx.found)
    if files_name is None:
        raise PushErr("The setup names no `scoter: values: project-files-dir`.")
    link = ctx.link()
    if link is None:
        raise PushErr("This project has no configuration to push: `scoter pull` or `scoter init` makes one.")
    if link.unlocked is not None:
        raise PushErr("Your configuration is unlocked: `scoter lock` reviews it first; only reviewed files are pushed.")
    chosen = (
        configs.find_config(ctx.verified, link, name) if name is not None
        else None if link.in_use is None else configs.load_config(ctx.verified, link.id, link.in_use)
    )
    if chosen is None:
        raise PushErr(f"There's no configuration {name or 'in use'} to push.")
    project_dir = Path(ctx.project_dir)
    _check_project_dir(config, project_dir)
    target = project_dir / files_name
    limits = lookup.limits(config)
    first = not os.path.lexists(target)
    live = snapshot.Manifest({})
    if not first:
        st = os.lstat(target)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise PushErr(f"{target} isn't a directory (or it's a symlink); push won't write through it.")
        try:
            live = (
                snapshot.snapshot_source(target, file_name, limits) if (target / file_name).is_file()
                else snapshot.snapshot_dir(target, limits)
            ).manifest
        except snapshot.SnapshotErr as err:
            raise PushErr(f"The project's files can't be read safely, so push won't write into them: {err}") from err
    contents = snapshot.from_store(ctx.verified, chosen.snapshot).contents
    changes: list[Change] = []
    for path in sorted(set(chosen.snapshot.files) | set(live.files)):
        mine, theirs = chosen.snapshot.files.get(path), live.files.get(path)
        if mine is None:
            kind = "extra"
        elif theirs is None:
            kind = "added"
        elif theirs.sha256 == mine.sha256 and theirs.executable == mine.executable:
            kind = "same"
        else:
            kind = "changed"
        changes.append(Change(path, kind, None if theirs is None else theirs.sha256, mine))
    pushed = Plan(project_dir, target, chosen, contents, changes, first)
    if not first:
        _mark_uncommitted(config, target, pushed)
    _patch(config, pushed, files_name, limits)
    return pushed


def _check_project_dir(config: UserConfig, project_dir: Path) -> None:
    """Refuse a project directory that's a symlink, or inside the tools' own places (VER-39)."""
    st = os.lstat(project_dir)
    if stat.S_ISLNK(st.st_mode) or Path(os.path.realpath(project_dir)) != project_dir:
        raise PushErr(f"{project_dir} is, or goes through, a symlink; push won't write through it.")
    for section, key in (("scoter", "state-dir"), ("verification", "store"), ("pull", "unlocked-dir")):
        place = config.optional_path(section, key)
        if place is not None:
            real = Path(os.path.realpath(place))
            if project_dir == real or project_dir.is_relative_to(real):
                raise PushErr(f"{project_dir} is inside `{section}: {key}` ({real}); push won't write there.")


def _mark_uncommitted(config: UserConfig, target: Path, pushed: Plan) -> None:
    """Mark the files the push overwrites that hold changes nobody committed."""
    found_project = config.found_project()
    if found_project is None or found_project.vcs is None or found_project.vcs_root is None:
        return
    try:
        report = vcs_files.file_status(
            config, found_project.vcs, found_project.vcs_root, seed.DEFAULT_REF, target, target,
        )
    except (OSError, vcs_files.VcsErr) as err:
        pushed.warnings.append(f"The VCS couldn't say what's committed ({review.clean(str(err))}).")
        return
    for change in pushed.changes:
        status = report.files.get(change.path)
        if change.kind == "changed" and status is not None and status.status in (
            vcs_files.Status.MODIFIED, vcs_files.Status.UNTRACKED,
        ):
            change.uncommitted = True


def _local_markers(config: UserConfig) -> list[tuple[str, str]]:
    """Text that names this computer: (text, why)."""
    found: list[tuple[str, str]] = []
    home = os.path.expanduser("~")
    if home and home != "~" and len(home) > 1:
        found.append((home, "your home directory"))
    host = socket.gethostname()
    if host and len(host) > 2:
        found.append((host, "this computer's name"))
    return found


def _patch(config: UserConfig, pushed: Plan, files_name: str, limits: snapshot.SnapshotLimits) -> None:
    """The patch, in `git diff` format, with VER-15's and VER-16's notes beside it."""
    markers = _local_markers(config)
    file_checks = checks.simple_checks(config, checks.PROJECT_FILES)
    out = pushed.patch
    for change in pushed.changes:
        rel = f"{files_name}/{change.path}"
        shown = review.visible(rel)
        if change.kind == "same":
            continue
        if change.kind == "extra":
            out.append(f"# {shown}: in the project, not in your configuration; push leaves it, for you to remove with your VCS.")
            continue
        assert change.entry is not None  # nosec: B101  # only extras have none
        new = pushed.contents[change.path]
        old = _read_live(pushed.target, change.path, limits) if change.kind == "changed" else None
        mode = "100755" if change.entry.executable else "100644"
        out.append(f"diff --git a/{shown} b/{shown}")
        if change.kind == "added":
            out.append(f"new file mode {mode}")
        if change.uncommitted:
            out.append(f"# {shown}: the project's copy has changes nobody committed; the push overwrites them.")
        for check in file_checks:
            if checks.file_like(change.path, check.matches, False):
                warning = f"{shown}: {review.clean(check.reason)} (`{check.id}`)"
                out.append(f"# warning: {warning}")
                pushed.warnings.append(warning)
        new_lines, old_lines = _text(new), (None if old is None else _text(old))
        if new_lines is None or (old is not None and old_lines is None):
            out.append(f"Binary files {'a/' + shown if old is not None else '/dev/null'} and b/{shown} differ")
            continue
        diff = difflib.unified_diff(
            old_lines or [], new_lines,
            f"a/{shown}" if old is not None else "/dev/null", f"b/{shown}", lineterm="",
        )
        for line in diff:
            out.append(line)
            if line.startswith("+") and not line.startswith("+++"):
                for text, why in markers:
                    if text in line:
                        warning = f"{shown}: a line names {why} ({review.clean(text)})"
                        out.append(f"# warning: {warning}")
                        pushed.warnings.append(warning)


def _read_live(target: Path, rel: str, limits: snapshot.SnapshotLimits) -> bytes | None:
    try:
        fd = _open_parent(target, rel)
    except OSError:
        return None
    try:
        name = rel.rsplit("/", 1)[-1]
        file_fd = os.open(name, os.O_RDONLY | fileutil.O_NOFOLLOW | fileutil.O_BINARY, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as inp:
            return inp.read(limits.max_file_size + 1)
    except OSError:
        return None
    finally:
        os.close(fd)


# ---------------------------------------------------------------------
# Writing


def _open_parent(target: Path, rel: str, *, create: bool = False) -> int:
    """A descriptor for `rel`'s directory under `target`, every part opened with `O_NOFOLLOW`."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | fileutil.O_NOFOLLOW
    fd = os.open(target, flags)
    try:
        for part in rel.split("/")[:-1]:
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, MODE_DIR, dir_fd=fd)
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write_one(target: Path, change: Change, data: bytes) -> bool:
    """Write one file, if it still has the hash the patch showed; whether it did."""
    assert change.entry is not None  # nosec: B101  # only writes
    snapshot.check_name(change.path)
    snapshot.check_relative(change.path)
    fd = _open_parent(target, change.path, create=True)
    name = change.path.rsplit("/", 1)[-1]
    temp = f"{TEMP_PREFIX}{name}"
    try:
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            st = None
        if st is None:
            if change.live_sha256 is not None:
                return False
        else:
            if not stat.S_ISREG(st.st_mode) or change.live_sha256 is None:
                return False
            file_fd = os.open(name, os.O_RDONLY | fileutil.O_NOFOLLOW | fileutil.O_BINARY, dir_fd=fd)
            with os.fdopen(file_fd, "rb") as inp:
                if _sha256(inp.read()) != change.live_sha256:
                    return False
        mode = 0o755 if change.entry.executable else 0o644
        out_fd = os.open(
            temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | fileutil.O_NOFOLLOW | fileutil.O_BINARY, mode, dir_fd=fd,
        )
        try:
            with os.fdopen(out_fd, "wb") as out:
                os.fchmod(out.fileno(), mode)
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
        except BaseException:
            try:
                os.unlink(temp, dir_fd=fd)
            except OSError:
                pass
            raise
        return True
    finally:
        os.close(fd)


def _first_push(pushed: Plan, limits: snapshot.SnapshotLimits) -> None:
    """Write every file into a new directory beside `.surf-scoter.d`, then rename it into place (VER-39)."""
    temp = Path(tempfile.mkdtemp(dir=pushed.project_dir, prefix=TEMP_PREFIX))
    try:
        snapshot.write_files(
            pushed.config.snapshot, temp, limits, lambda rel, _: pushed.contents[rel],
        )
        os.chmod(temp, MODE_DIR)
        # A rename onto an empty directory would succeed: check it's still not there.
        if os.path.lexists(pushed.target):
            raise PushErr(f"{pushed.target} appeared while pushing; nothing was written.  Push again.")
        os.rename(temp, pushed.target)
    except BaseException:
        fileutil.rm(temp)
        raise


def write(config: UserConfig, pushed: Plan) -> Result:
    """Write the plan's files, then make the configuration pushed the link's base."""
    if fileutil.is_windows():
        raise PushErr("Push needs directory descriptors, which Windows doesn't have; it isn't supported there yet.")
    limits = lookup.limits(config)
    result = Result(extras=[c.path for c in pushed.extras])
    if pushed.first:
        _first_push(pushed, limits)
        result.written = sorted(pushed.config.snapshot.files)
    else:
        for change in pushed.writes:
            if _write_one(pushed.target, change, pushed.contents[change.path]):
                result.written.append(change.path)
            else:
                result.refused.append(change.path)
    ctx = lifecycle.context(config)
    with ctx.verified.locked():
        link = ctx.link()
        if link is not None:
            # As the project's files would be with only the configuration in
            # them: what the run compares with (VER-21).
            plain = snapshot.Manifest({p: replace(e, canonical=None) for p, e in pushed.config.snapshot.files.items()})
            link.base = pushed.config.snapshot
            link.provenance = {**(link.provenance or {}), "source-id": plain.id, "pushed": pushed.config.id}
            configs.save_link(ctx.verified, link)
    return result


def push(
    config: UserConfig,
    *,
    name: str | None = None,
    yes: bool = False,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> int:
    """Show the patch, ask, and write: 0 when the push wrote everything it meant to."""
    pushed = plan(config, name)
    if not pushed.writes and not pushed.first:
        say("Nothing to push: the project's files already match your configuration.")
        for change in pushed.extras:
            say(f"  (The project also has {review.visible(change.path)}, which your configuration doesn't.)")
        return 0
    if yes:
        for warning in pushed.warnings:
            say(f"Warning: {warning}")
    else:
        for line in pushed.patch:
            say(line)
        try:
            answer = ask(f"Write these files into {pushed.target}?  Type `yes`; Enter cancels: ")
        except EOFError:
            answer = ""
        if answer.strip() != "yes":
            say("Nothing was written.")
            return 1
    result = write(config, pushed)
    say(f"Wrote {len(result.written)} file(s) into {pushed.target}; review and commit them with your VCS.")
    for path in result.refused:
        say(f"  Not written: {review.visible(path)} changed after the patch was made; push again to see it.")
    for path in result.extras:
        say(f"  Left: {review.visible(path)}, which your configuration doesn't have.")
    return 1 if result.refused else 0

