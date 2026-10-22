#SPDX:MIT
"""File helper utilities."""
import contextlib
import enum
import os
import platform
import shutil
import tempfile
import time
from collections.abc import Callable, Generator, Sequence
from pathlib import Path

from . import log
from .err import LockTimeoutErr, SurfScoterErr

# ---------------------------------------------------------------------
# Basic file operations

def backup(file: Path, backup_ext: str) -> bool:
    """Backup the file.
    
    Returns 'True' if the backup was successful.
    """
    if not file.is_file():
        # The file to back up doesn't exist, so there's nothing to back up.
        # This is fine.
        return True
    to_file = Path(str(file) + backup_ext)
    if to_file.exists():
        rm(to_file)
    # Refresh metadata in the check.
    if not os.path.exists(to_file):
        file.copy(to_file, preserve_metadata=True)
        return True
    return False


class WithoutBackup(enum.Enum):
    """What `restore` does with the file when it has no backup."""

    KEEP = "keep"
    DELETE = "delete"


def restore(
    file: Path, backup_ext: str, without_backup: WithoutBackup = WithoutBackup.KEEP,
) -> bool:
    """Restore the file from backup.

    Returns 'True' if the restore succeeded.

    With `WithoutBackup.DELETE`, the restore removes the file if there
    exists no backup.

    If the file can't be removed, then the backup goes untouched.
    """
    from_file = Path(str(file) + backup_ext)
    if not from_file.exists():
        # There is no backup file.
        # ... only delete the source file if the caller requests it.
        if without_backup == WithoutBackup.DELETE and not rm(file):  # noqa: SIM103  # Keeps the comment on the failing case.
            # It still exists.  Can't restore from backup.
            return False
        return True
    try:
        from_file.move(file)
    except OSError:
        return False
    return True


MODE_USER_RW = 0o600
MODE_USER_R_ONLY = 0o400
MODE_USER_RWX = 0o700
# The mode bits that give the group or others any access.
MODE_NOT_PRIVATE = 0o077


def touch(file: Path, mode: int) -> None:
    """Ensure the file exists with the given mode."""
    file.parent.mkdir(exist_ok=True, parents=True)
    file.touch(mode)
    os.chmod(file, mode)


def rm(file: Path) -> bool:
    """Try to remove the file.  Does not raise an error, but reports it to stderr.
    
    If the file is a directory, it will try to remove it recursively.
    """
    if not file.exists():
        return True
    if file.is_mount():
        log.warning(f"Warning: cannot delete mount point at {file}")
        return False
    try:
        if (
            file.is_file()
            or file.is_symlink()
            or file.is_junction()
            or file.is_char_device()
            or file.is_socket()
        ):
            file.unlink()
        elif file.is_dir():
            shutil.rmtree(file)
        else:
            log.warning(f"Warning: could not delete unsupported file type at {file}")
            return False

        # Final check if things worked.
        if file.exists():
            log.warning(f"Warning: could not delete {file}")
            return False
        return True
    except PermissionError:
        log.warning(f"Warning: permission issue deleting {file}")
        return not file.exists()
    except OSError as err:
        log.warning(f"Warning: {err}")
        return not file.exists()


def find_exe(name: str | Path) -> Path | None:
    """Find the executable."""
    # Which will work if you pass it the absolute location.
    loc = shutil.which(name)
    if loc:
        # If 'name' is relative, which will return a relative path.
        return Path(os.path.abspath(loc))
    if is_windows():
        # Check for a missing PATHEXT.
        path_ext = os.environ.get("PATHEXT", "").split(os.path.pathsep)
        for ext in path_ext:
            if not ext:
                continue
            if str(name).endswith(ext) and os.path.isfile(name):
                return Path(os.path.abspath(name))
        for ext in path_ext:
            if not ext:
                continue
            if os.path.isfile(str(name) + ext):
                return Path(os.path.abspath(name))
        # Not a Windows executable.
        return None
    if os.path.isfile(name) and os.access(name, os.X_OK):
        return Path(os.path.abspath(name))
    # Not a non-Windows executable.
    return None


def require_exe(name: str | Path) -> Path:
    """Require that the executable exists."""
    ret = find_exe(name)
    if not ret:
        raise SurfScoterErr(f"could not find program '{name}'")
    return ret


def is_windows() -> bool:
    """Check if the user is running on Windows."""
    return platform.system() == "Windows"


# ---------------------------------------------------------------------
# Private files


def private_dir(root: Path, parts: Sequence[str]) -> Path:
    """The directory `root/parts...`, private to the user.

    `root` and the directories below it are created with mode 0700 as
    needed, and made 0700 if they exist; none of them may be a symlink.
    """
    if not root.is_absolute():
        raise SurfScoterErr(f"{root}: must be an absolute path")
    if not os.path.lexists(root):
        root.mkdir(mode=MODE_USER_RWX, parents=True)
    directory = root
    for part in ("", *parts):
        if part:
            if part in (".", "..") or "/" in part or "\\" in part:
                raise SurfScoterErr(f"`{part}` is not a directory name")
            directory = directory / part
            if not os.path.lexists(directory):
                directory.mkdir(mode=MODE_USER_RWX)
        if os.path.islink(directory) or not directory.is_dir():
            raise SurfScoterErr(f"{directory}: not a directory (or a symlink)")
        if not is_windows():
            os.chmod(directory, MODE_USER_RWX)
    return directory


def write_private(root: Path, parts: Sequence[str], data: bytes) -> Path:
    """Write `data` to `root/parts...`, private to the user, replacing any file whole.

    `root` and the directories below it are created with mode 0700 as
    needed; none of them may be a symlink.  The file is written to a
    temporary file beside it (mode 0600) and renamed into place.
    """
    path = private_dir(root, parts[:-1]) / parts[-1]
    directory = path.parent
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise
    return path


# ---------------------------------------------------------------------
# Locking

# Flags for opening a file without following a symlink, where the platform can.
O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
O_BINARY: int = getattr(os, "O_BINARY", 0)
# Opening a FIFO without waiting for a writer, where the platform can.
O_NONBLOCK: int = getattr(os, "O_NONBLOCK", 0)


@contextlib.contextmanager
def exclusive_lock(
    path: Path,
    *,
    timeout: float = 30.0,
    poll: float = 0.2,
    on_wait: Callable[[], None] | None = None,
    check: Callable[[int], None] | None = None,
) -> Generator[None]:
    """Hold an operating system lock on `path` (created if needed) while in the block.

    The operating system lets the lock go when the process ends, however it
    ends, so a crashed run never leaves it held.  POSIX uses `flock`, which
    locks the open file: two opens conflict, even in one process.  Windows
    locks the file's first byte with `msvcrt.locking`.

    It doesn't block: it tries every `poll` seconds, calling `on_wait` once
    the first time it has to wait, and raises LockTimeoutErr after `timeout`
    seconds.  `check` is given the open file's descriptor before locking, to
    check its owner and mode.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT | O_NOFOLLOW | O_BINARY, MODE_USER_RW)
    try:
        if check:
            check(fd)
        deadline = time.monotonic() + timeout
        waited = False
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise LockTimeoutErr(
                    f"{path}: another Surf Scoter command held the lock for more than "
                    f"{timeout:g} seconds"
                )
            if not waited and on_wait:
                on_wait()
            waited = True
            time.sleep(poll)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


if is_windows():
    import msvcrt  # Windows only.

    def _try_lock(fd: int) -> bool:
        """Take the lock on the open file without waiting; False if another holds it."""
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
else:
    import fcntl  # POSIX only.

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
