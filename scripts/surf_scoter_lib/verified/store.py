#SPDX:MIT
"""The verified store: the user's accepted project settings, in `verification: paths: store`.

    store/                        0700
        index.json                0600  canonical project directory -> environment id
        lock                      0600  held while anything below changes
        blobs/AB/ABCD...          0600  file contents, by SHA-256
        ENV-ID/environment.json   0600  the environment's acceptances
        ENV-ID/acceptances/ID/... 0600  each acceptance's files

Everything in it decides what a project may do, so every directory and file
must belong to the user, with no access for anyone else, and no symlinks.
Every read checks that; every write replaces the file whole.  On Windows,
which has no mode bits, the checks are left to the user profile's ACLs.
"""
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Generator
from pathlib import Path

from .. import fileutil, jdata
from ..err import VerifiedStoreErr
from ..user_config import UserConfig

# Internal storage file names can be hard-coded.
INDEX_SCHEMA = "surf-scoter-verified-index-1"
INDEX_FILE = "index.json"
LOCK_FILE = "lock"
BLOBS_DIR = "blobs"

MODE_DIR = fileutil.MODE_USER_RWX
MODE_FILE = fileutil.MODE_USER_RW

# The store's own JSON files are small; this is only a guard.
MAX_JSON_SIZE = 16 * 1024 * 1024

_NAME_CHARS = re.compile(r"[^a-z0-9-]+")
_HEX = re.compile(r"[0-9a-f]{64}")


def environment_id(project_dir: Path) -> str:
    """The environment's id: `NAME-HASH`, from the project directory's canonical path.

    `NAME` is the directory's name, lower-cased, with anything outside
    `[a-z0-9-]` made `-`, cut to 32 characters (`project` if that leaves
    nothing).  `HASH` is the first 12 hex digits of the SHA-256 of the
    canonical path.  The same directory always gets the same id, with no
    stored state.
    """
    canonical = os.path.realpath(project_dir)
    name = _NAME_CHARS.sub("-", os.path.basename(canonical).lower())[:32].strip("-")
    digest = hashlib.sha256(os.fsencode(canonical)).hexdigest()[:12]
    return f"{name or 'project'}-{digest}"


def project_id(config: UserConfig, project_dir: str) -> str:
    """The project's id: the generator's (`scoter:project-id`), as the setup's paths use it.

    Worked out the same way (`environment_id`) for a universal configuration
    file without one, such as a test's.
    """
    given = config.raw("active-config-locations", "project-id")
    return given if isinstance(given, str) and given else environment_id(Path(project_dir))


def canonical_json(data: jdata.JsonData) -> bytes:
    """`data` as canonical JSON: sorted keys, no spaces, ASCII."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def json_id(data: jdata.JsonData) -> str:
    """The id of JSON data: `sha256:` and the hash of its canonical JSON."""
    return "sha256:" + hashlib.sha256(canonical_json(data)).hexdigest()


def _is_windows() -> bool:
    return sys.platform == "win32"


def check_private(st: os.stat_result, path: Path) -> None:
    """Refuse a store entry another user owns, or that others can use."""
    if _is_windows():
        return
    if st.st_uid != os.getuid():
        raise VerifiedStoreErr(f"{path}: belongs to another user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise VerifiedStoreErr(
            f"{path}: others can use it (mode {stat.S_IMODE(st.st_mode):o}); "
            f"the verified store must be private to you"
        )


class VerifiedStore:
    """The verified store at `root`.

    Use `open` to get one.  Changes need the lock: `with store.locked(): ...`.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._locked = False

    @staticmethod
    def open(root: Path, *, create: bool = False) -> "VerifiedStore":
        """The store at `root`, which must be an absolute path.

        With `create`, a missing store is made (its parents as needed).
        """
        if not root.is_absolute():
            raise VerifiedStoreErr(f"{root}: the verified store must be an absolute path")
        if create and not os.path.lexists(root):
            root.mkdir(mode=MODE_DIR, parents=True)
            os.chmod(root, MODE_DIR)
        store = VerifiedStore(root)
        store._check_dir(root)
        return store

    # ---- locking

    @contextlib.contextmanager
    def locked(self, timeout: float = 30.0) -> Generator[None]:
        """Hold the store's lock."""
        def waiting() -> None:
            print(f"Waiting for another Surf Scoter command to finish with {self.root} ...")

        def check(fd: int) -> None:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise VerifiedStoreErr(f"{self.root / LOCK_FILE}: not a regular file")
            check_private(st, self.root / LOCK_FILE)

        with fileutil.exclusive_lock(
            self.root / LOCK_FILE, timeout=timeout, on_wait=waiting, check=check,
        ):
            self._locked = True
            try:
                yield
            finally:
                self._locked = False

    def _require_lock(self) -> None:
        if not self._locked:
            raise RuntimeError("the verified store must be locked to change it")

    # ---- paths

    def _check_dir(self, path: Path) -> None:
        try:
            st = os.lstat(path)
        except FileNotFoundError as err:
            raise VerifiedStoreErr(f"{path}: doesn't exist") from err
        if stat.S_ISLNK(st.st_mode):
            raise VerifiedStoreErr(f"{path}: is a symlink; the verified store has none")
        if not stat.S_ISDIR(st.st_mode):
            raise VerifiedStoreErr(f"{path}: not a directory")
        check_private(st, path)

    def _dir(self, parts: tuple[str, ...], *, create: bool) -> Path | None:
        """The store directory `parts`, each part checked; None if it's missing."""
        current = self.root
        for part in parts:
            _check_part(part)
            current = current / part
            if not os.path.lexists(current):
                if not create:
                    return None
                current.mkdir(mode=MODE_DIR)
                os.chmod(current, MODE_DIR)
            self._check_dir(current)
        return current

    # ---- files

    def read_bytes(self, *parts: str, max_size: int) -> bytes | None:
        """The store file `parts`, or None if it's missing."""
        directory = self._dir(parts[:-1], create=False)
        if directory is None:
            return None
        _check_part(parts[-1])
        path = directory / parts[-1]
        try:
            fd = os.open(path, os.O_RDONLY | fileutil.O_NOFOLLOW | fileutil.O_BINARY)
        except FileNotFoundError:
            return None
        except OSError as err:
            # ELOOP: a symlink.
            raise VerifiedStoreErr(f"{path}: can't be opened ({err})") from err
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise VerifiedStoreErr(f"{path}: not a regular file")
            check_private(st, path)
            if st.st_size > max_size:
                raise VerifiedStoreErr(f"{path}: larger than {max_size} bytes")
            with os.fdopen(fd, "rb", closefd=False) as inp:
                data = inp.read(max_size + 1)
            if len(data) > max_size:
                raise VerifiedStoreErr(f"{path}: larger than {max_size} bytes")
            return data
        finally:
            os.close(fd)

    def write_bytes(self, *parts: str, data: bytes) -> Path:
        """Replace the store file `parts` with `data`, whole."""
        self._require_lock()
        directory = self._dir(parts[:-1], create=True)
        assert directory is not None  # nosec: B101  # created above.
        _check_part(parts[-1])
        path = directory / parts[-1]
        fd, temp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
        try:
            os.chmod(temp, MODE_FILE)
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

    def remove(self, *parts: str) -> None:
        """Remove the store file or directory `parts`, if it's there."""
        self._require_lock()
        directory = self._dir(parts[:-1], create=False)
        if directory is None:
            return
        _check_part(parts[-1])
        path = directory / parts[-1]
        if not os.path.lexists(path):
            return
        if os.path.isdir(path) and not os.path.islink(path):
            self._check_dir(path)
            # rmtree doesn't follow symlinks.
            shutil.rmtree(path)
        else:
            os.unlink(path)

    def list_dir(self, *parts: str) -> list[str]:
        """The names in the store directory `parts` (empty if it's missing)."""
        directory = self._dir(parts, create=False)
        if directory is None:
            return []
        return sorted(name for name in os.listdir(directory) if not name.startswith(".tmp-"))

    def read_json(self, *parts: str, schema: str) -> jdata.DictJsonData | None:
        """The store JSON file `parts`, checked for its `$schema`; None if it's missing."""
        data = self.read_bytes(*parts, max_size=MAX_JSON_SIZE)
        if data is None:
            return None
        where = self.root.joinpath(*parts)
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise VerifiedStoreErr(f"{where}: not a JSON file ({err})") from err
        if not isinstance(value, dict) or value.get("$schema") != schema:
            raise VerifiedStoreErr(f"{where}: not a `{schema}` file")
        return value

    def write_json(self, *parts: str, data: jdata.DictJsonData) -> Path:
        """Replace the store JSON file `parts`."""
        text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
        return self.write_bytes(*parts, data=text.encode("ascii"))

    # ---- the index

    def index(self) -> dict[str, str]:
        """Each canonical project directory, with its environment id."""
        data = self.read_json(INDEX_FILE, schema=INDEX_SCHEMA)
        if data is None:
            return {}
        try:
            return {
                str(path_key[-1]): jdata.req_str(data, *path_key, "id")
                for path_key in jdata.opt_dict_keys(data, "environments")
            }
        except (KeyError, TypeError) as err:
            raise VerifiedStoreErr(f"{self.root / INDEX_FILE}: {err}") from err

    def register(self, project_dir: str, env_id: str) -> None:
        """Add the environment to the index."""
        self._require_lock()
        index = self.index()
        known = index.get(project_dir)
        if known is not None and known != env_id:
            raise VerifiedStoreErr(
                f"{self.root / INDEX_FILE}: lists {project_dir} as `{known}`, not `{env_id}`"
            )
        for other, other_id in index.items():
            if other_id == env_id and other != project_dir:
                raise VerifiedStoreErr(
                    f"{self.root / INDEX_FILE}: `{env_id}` is already {other}, not {project_dir}"
                )
        index[project_dir] = env_id
        self._write_index(index)

    def unregister(self, project_dir: str) -> None:
        """Remove the environment from the index."""
        self._require_lock()
        index = self.index()
        if index.pop(project_dir, None) is not None:
            self._write_index(index)

    def _write_index(self, index: dict[str, str]) -> None:
        self.write_json(INDEX_FILE, data={
            "$schema": INDEX_SCHEMA,
            "environments": {path: {"id": env_id} for path, env_id in sorted(index.items())},
        })

    # ---- blobs

    def put_blob(self, data: bytes) -> str:
        """Keep `data` as a blob; its SHA-256."""
        digest = hashlib.sha256(data).hexdigest()
        directory = self._dir((BLOBS_DIR, digest[:2]), create=False)
        # One already there is checked when it's read.
        if directory is None or not os.path.lexists(directory / digest):
            self.write_bytes(BLOBS_DIR, digest[:2], digest, data=data)
        return digest

    def get_blob(self, digest: str, max_size: int) -> bytes:
        """The blob `digest`, checked against its hash."""
        if not _HEX.fullmatch(digest):
            raise VerifiedStoreErr(f"`{digest}` is not a blob's SHA-256")
        data = self.read_bytes(BLOBS_DIR, digest[:2], digest, max_size=max_size)
        if data is None:
            raise VerifiedStoreErr(f"{self.root / BLOBS_DIR}: blob {digest} is missing")
        if hashlib.sha256(data).hexdigest() != digest:
            raise VerifiedStoreErr(f"{self.root / BLOBS_DIR}: blob {digest} has changed")
        return data

    def remove_unused_blobs(self, used: set[str]) -> int:
        """Remove the blobs not in `used`, and the prefix directories left empty; how many went."""
        self._require_lock()
        removed = 0
        for prefix in self.list_dir(BLOBS_DIR):
            for digest in self.list_dir(BLOBS_DIR, prefix):
                if digest not in used:
                    self.remove(BLOBS_DIR, prefix, digest)
                    removed += 1
            if not self.list_dir(BLOBS_DIR, prefix):
                self.remove(BLOBS_DIR, prefix)
        return removed

    def temp_files(self) -> list[Path]:
        """The temporary files writes leave behind when they're stopped part way (`.tmp-*`).

        Only a crash leaves one: while the store is locked, none is being written.
        """
        found: list[Path] = []
        for directory, dirs, files in os.walk(self.root):
            # `os.walk` doesn't follow directory symlinks; the store has none.
            found.extend(Path(directory) / name for name in files if name.startswith(".tmp-"))
            dirs.sort()
        return sorted(found)

    def remove_temp_files(self) -> int:
        """Remove the temporary files writes left behind; how many went."""
        self._require_lock()
        found = self.temp_files()
        for path in found:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
        return len(found)

    def blob_size(self) -> int:
        """How much the blobs take, in bytes."""
        total = 0
        for prefix in self.list_dir(BLOBS_DIR):
            directory = self._dir((BLOBS_DIR, prefix), create=False)
            if directory is None:
                continue
            for digest in self.list_dir(BLOBS_DIR, prefix):
                total += os.lstat(directory / digest).st_size
        return total


def _check_part(part: str) -> None:
    """Refuse a store path part that isn't a plain name."""
    if (
        not part
        or part in (".", "..")
        or "/" in part
        or "\\" in part
        or "\0" in part
        or ":" in part
    ):
        raise VerifiedStoreErr(f"`{part}` is not a name in the verified store")
