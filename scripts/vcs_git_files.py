#SPDX:MIT
"""Answer the pull's questions about a Git repository's committed files.

Usage: vcs_git_files.py GIT list-files ROOT REF PATH
       vcs_git_files.py GIT show-file ROOT COMMIT PATH
       vcs_git_files.py GIT file-status ROOT REF PATH FILES

The `git-vcs` setup section runs this as its `runs: list-files`, `show-file`
and `file-status`, with GIT the setup's `git`.  ROOT is your repository (never
the project clone), PATH a directory in it, relative to ROOT, and FILES a
copy of that directory's files.

* `list-files`: the files under PATH at REF, as JSON (`surf-scoter-vcs-files-1`):
  the commit REF is, and for each file (relative to PATH) its kind (`file`,
  `executable`, `symlink` or `submodule`), object id and size.
* `show-file`: the contents of PATH (a file) at COMMIT, a commit id
  `list-files` gave, so every file comes from the same commit.
* `file-status`: for each file in FILES, whether PATH holds the same file
  at REF, as JSON (`surf-scoter-vcs-file-status-1`): `committed`, `modified`
  (different contents or executable bit) or `untracked`, with the committed
  version's id.

Git only reads the repository's objects here: `rev-parse`, `ls-tree` and
`cat-file`.  Nothing reads the working tree through Git, which would run
`core.fsmonitor` and the filters `.gitattributes` names; `file-status` works
out each file's blob id itself.  Paths go through `-z` and as single
arguments, and pathspecs are literal.
"""
import json
import os
import stat
import subprocess  # nosec: B404  # Runs the setup's git, as a list.
import sys
from collections.abc import Sequence

from surf_scoter_lib.vcs_files import (
    FILES_SCHEMA,
    OBJECT_ID,
    STATUS_SCHEMA,
    Kind,
    Status,
    git_blob_id,
)

TIMEOUT_SECONDS = 60
_KINDS = {"100644": Kind.FILE, "100755": Kind.EXECUTABLE, "120000": Kind.SYMLINK, "160000": Kind.SUBMODULE}
USAGE = """\
Usage: vcs_git_files.py GIT list-files ROOT REF PATH
       vcs_git_files.py GIT show-file ROOT COMMIT PATH
       vcs_git_files.py GIT file-status ROOT REF PATH FILES"""


class HandlerErr(Exception):
    """The question can't be answered."""


def _git(git: str, root: str, *args: str) -> bytes:
    """Run Git in `root` with `args`; its output."""
    # A project can set Git's variables as you enter its directory.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_LITERAL_PATHSPECS"] = "1"
    cmd = [
        git, "-C", root, "--no-pager", "--no-optional-locks",
        "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
        *args,
    ]
    try:
        res = subprocess.run(  # nosec: B603  # The setup's git, as a list.
            cmd, capture_output=True, check=False, shell=False,
            env=env, timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as err:
        raise HandlerErr(f"git couldn't run: {err}") from err
    if res.returncode != 0:
        message = res.stderr.decode("utf-8", errors="replace").strip()
        raise HandlerErr(f"git {args[0]} failed: {message.encode('ascii', 'backslashreplace').decode()}")
    return res.stdout


def _commit(git: str, root: str, ref: str) -> str:
    """The commit `ref` names."""
    if not ref or ref.startswith("-"):
        raise HandlerErr(f"{ref!a} isn't a ref")
    out = _git(git, root, "rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}")
    commit = out.decode("ascii", errors="replace").strip()
    if not OBJECT_ID.fullmatch(commit):
        raise HandlerErr(f"{ref!a} isn't a commit")
    return commit


def _tree(git: str, root: str, commit: str, path: str) -> dict[str, tuple[Kind, str, int | None]]:
    """The files under `path` at `commit`: (kind, id, size), by path relative to `path`."""
    prefix = path.rstrip("/") + "/"
    out = _git(git, root, "ls-tree", "-r", "-z", "-l", "--full-tree", commit, "--", prefix)
    found: dict[str, tuple[Kind, str, int | None]] = {}
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, _, name_bytes = record.partition(b"\t")
        mode, kind, object_id, size = meta.decode("ascii").split()
        name = os.fsdecode(name_bytes)
        if not name.startswith(prefix):
            continue
        rel = name[len(prefix):]
        if mode not in _KINDS or kind not in ("blob", "commit"):
            raise HandlerErr(f"{rel!a}: an entry Git calls {mode} {kind}")
        found[rel] = (_KINDS[mode], object_id, None if size == "-" else int(size))
    return found


def list_files(git: str, root: str, ref: str, path: str) -> dict:
    """`list-files`."""
    commit = _commit(git, root, ref)
    return {
        "$schema": FILES_SCHEMA,
        "commit": commit,
        "files": {
            rel: {"kind": kind, "id": object_id, "size": size}
            for rel, (kind, object_id, size) in sorted(_tree(git, root, commit, path).items())
        },
    }


def show_file(git: str, root: str, commit: str, path: str) -> bytes:
    """`show-file`: only from a commit id, so it's the commit `list-files` gave."""
    if not OBJECT_ID.fullmatch(commit):
        raise HandlerErr(f"{commit!a} isn't a commit id")
    return _git(git, root, "cat-file", "blob", f"{commit}:{path}")


def file_status(git: str, root: str, ref: str, path: str, files: str) -> dict:
    """`file-status`."""
    commit = _commit(git, root, ref)
    tree = _tree(git, root, commit, path)
    report: dict[str, dict] = {}
    for directory, dirs, names in os.walk(files):
        dirs.sort()
        for name in sorted(names):
            full = os.path.join(directory, name)
            rel = os.path.relpath(full, files).replace(os.sep, "/")
            fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as inp:
                st = os.fstat(inp.fileno())
                if not stat.S_ISREG(st.st_mode):
                    raise HandlerErr(f"{rel!a}: not a regular file")
                data = inp.read()
            kind = Kind.EXECUTABLE if st.st_mode & stat.S_IXUSR else Kind.FILE
            committed = tree.get(rel)
            if committed is None:
                report[rel] = {"status": Status.UNTRACKED, "id": None}
            elif committed[0] == kind and committed[1] == git_blob_id(data, commit):
                report[rel] = {"status": Status.COMMITTED, "id": committed[1]}
            else:
                report[rel] = {"status": Status.MODIFIED, "id": committed[1]}
    return {"$schema": STATUS_SCHEMA, "commit": commit, "files": report}


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    args = list(argv[1:])
    counts = {"list-files": 5, "show-file": 5, "file-status": 6}
    if len(args) < 2 or counts.get(args[1]) != len(args):
        print(USAGE, file=sys.stderr)
        return 2
    git, action = args[0], args[1]
    try:
        if action == "show-file":
            sys.stdout.buffer.write(show_file(git, *args[2:]))
            return 0
        report = list_files(git, *args[2:]) if action == "list-files" else file_status(git, *args[2:])
    except (HandlerErr, OSError, ValueError) as err:
        print(f"vcs_git_files.py: {err}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
