#SPDX:MIT
"""Test the vcs_files module, with the Git handler (`vcs_git_files.py`) and handlers that misbehave."""
import os
import shutil
import stat
import subprocess  # nosec: B404  # Makes the tests' Git repositories.
import sys
import unittest
from pathlib import Path
from unittest import mock

from ...surf_scoter_lib import err, vcs_files
from ...surf_scoter_lib.user_config import UserConfig
from ...surf_scoter_lib.verified import snapshot
from .verified.helper import POSIX, TempDirTest

SCRIPTS = Path(__file__).parents[2]
HANDLER = SCRIPTS / "vcs_git_files.py"
GIT = shutil.which("git")
LIMITS = snapshot.SnapshotLimits(max_file_size=1000, max_snapshot_size=3000)


def _handler(*args: str) -> list[str]:
    # As `python: runs: script` runs it: the script's directory has the library.
    return [sys.executable, "-E", "-s", str(HANDLER), str(GIT), *args]


def git_config(runs: dict[str, list[str]] | None = None) -> UserConfig:
    """A universal file with a `git-vcs` section running the distribution's handler (or `runs`)."""
    if runs is None:
        runs = {
            "list-files": _handler("list-files", "{x:root}", "{x:ref}", "{x:path}"),
            "show-file": _handler("show-file", "{x:root}", "{x:commit}", "{x:path}"),
            "file-status": _handler("file-status", "{x:root}", "{x:ref}", "{x:path}", "{x:files}"),
        }
    return UserConfig({"resolved": {"git-vcs": {"runs": runs, "value-lists": {"drop-env": ["GIT_*"]}}}})


def fake(output: str, code: int = 0) -> list[str]:
    """A handler that prints `output` (Python string syntax) and exits with `code`."""
    return [sys.executable, "-I", "-c", f"import sys; sys.stdout.write({output}); sys.exit({code})"]


@unittest.skipUnless(POSIX and GIT, "needs git")
class GitTest(TempDirTest):
    """The Git handler, through the library, on a real repository."""

    def git(self, *args: str) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        return subprocess.run(  # nosec: B603  # git, as a list.
            [str(GIT), "-C", str(self.repo), *args], check=True, capture_output=True, env=env,
        ).stdout.decode()

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "T")
        self.files = self.repo / "proj" / ".surf-scoter.d"
        self.write("repo/proj/.surf-scoter.d/project.yaml", "container: {dev-container-path: env}\n")
        self.write("repo/proj/.surf-scoter.d/env/Dockerfile", "FROM scratch\n")
        self.write("repo/proj/.surf-scoter.d/env/run.sh", "#!/bin/sh\n", 0o755)
        self.write("repo/proj/other.txt", "not in the files directory\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "first")
        self.config = git_config()

    def test_list_and_show(self) -> None:
        """Every file under the directory, from one commit, with its contents."""
        listing = vcs_files.list_files(self.config, "git", self.repo, "HEAD", "proj/.surf-scoter.d")
        self.assertEqual(listing.commit, self.git("rev-parse", "HEAD").strip())
        self.assertEqual(sorted(listing.files), ["env/Dockerfile", "env/run.sh", "project.yaml"])
        self.assertEqual(listing.files["env/run.sh"].kind, vcs_files.Kind.EXECUTABLE)
        self.assertEqual(listing.files["env/Dockerfile"].size, len("FROM scratch\n"))
        data = vcs_files.show_file(
            self.config, "git", self.repo, listing.commit, "proj/.surf-scoter.d/env/Dockerfile", 100,
        )
        self.assertEqual(data, b"FROM scratch\n")
        self.assertEqual(
            vcs_files.git_blob_id(data, listing.commit),
            self.git("hash-object", "proj/.surf-scoter.d/env/Dockerfile").strip(),
        )
        with self.assertRaisesRegex(vcs_files.VcsErr, "isn't a ref"):
            vcs_files.list_files(self.config, "git", self.repo, "--output=/tmp/x", "proj")
        with self.assertRaisesRegex(vcs_files.VcsErr, "isn't a commit id"):
            vcs_files.show_file(self.config, "git", self.repo, "HEAD", "proj/x", 100)

    def test_snapshot_commit(self) -> None:
        """The committed files, not the working tree's, as a source snapshot."""
        self.write("repo/proj/.surf-scoter.d/env/Dockerfile", "FROM changed\n")
        self.write("repo/proj/.surf-scoter.d/new.txt", "uncommitted\n")
        commit, taken = vcs_files.snapshot_commit(self.config, "git", self.repo, "HEAD", self.files, LIMITS)
        self.assertEqual(commit, self.git("rev-parse", "HEAD").strip())
        self.assertEqual(sorted(taken.contents), ["env/Dockerfile", "env/run.sh", "project.yaml"])
        self.assertEqual(taken.contents["env/Dockerfile"], b"FROM scratch\n")
        self.assertTrue(taken.manifest.files["env/run.sh"].executable)

    def test_snapshot_commit_refused(self) -> None:
        """A committed symlink, or files over the limits, are refused before anything's read."""
        os.symlink("/etc/passwd", self.files / "env" / "creds")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "link")
        with self.assertRaisesRegex(vcs_files.VcsErr, "a symlink"):
            vcs_files.snapshot_commit(self.config, "git", self.repo, "HEAD", self.files, LIMITS)
        self.git("rm", "-q", "proj/.surf-scoter.d/env/creds")
        self.write("repo/proj/.surf-scoter.d/env/big", "x" * 1001)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "big")
        with self.assertRaisesRegex(vcs_files.VcsErr, "larger than the 1000 bytes"):
            vcs_files.snapshot_commit(self.config, "git", self.repo, "HEAD", self.files, LIMITS)

    def test_odd_names(self) -> None:
        """A committed name with a newline comes through whole, and is refused (VER-30)."""
        self.write("repo/proj/.surf-scoter.d/env/a\nb", "x")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "odd")
        with self.assertRaisesRegex(vcs_files.VcsErr, "control or non-ASCII"):
            vcs_files.list_files(self.config, "git", self.repo, "HEAD", "proj/.surf-scoter.d")

    def test_file_status(self) -> None:
        """Committed, modified (contents or executable bit) and untracked, against the commit."""
        copy = self.tmp / "copy"
        shutil.copytree(self.files, copy)
        report = vcs_files.file_status(self.config, "git", self.repo, "HEAD", self.files, copy)
        self.assertEqual({s.status for s in report.files.values()}, {vcs_files.Status.COMMITTED})
        self.assertEqual(report.commit, self.git("rev-parse", "HEAD").strip())
        (copy / "env" / "Dockerfile").write_text("FROM other\n", encoding="utf-8")
        os.chmod(copy / "project.yaml", os.stat(copy / "project.yaml").st_mode | stat.S_IXUSR)
        (copy / "new.txt").write_text("new\n", encoding="utf-8")
        report = vcs_files.file_status(self.config, "git", self.repo, "HEAD", self.files, copy)
        statuses = {path: s.status for path, s in report.files.items()}
        self.assertEqual(statuses, {
            "env/Dockerfile": vcs_files.Status.MODIFIED,
            "env/run.sh": vcs_files.Status.COMMITTED,
            "project.yaml": vcs_files.Status.MODIFIED,
            "new.txt": vcs_files.Status.UNTRACKED,
        })
        self.assertIsNone(report.files["new.txt"].id)
        self.assertEqual(report.to_json()["files"]["new.txt"], {"status": "untracked", "id": None})

    def test_no_working_tree_reads(self) -> None:
        """The repository's `core.fsmonitor` and filters never run."""
        marker = self.tmp / "ran"
        hook = self.write("hook.sh", f"#!/bin/sh\ntouch {marker}\ncat\n", 0o755)
        self.git("config", "core.fsmonitor", str(hook))
        self.git("config", "filter.evil.clean", str(hook))
        self.git("config", "filter.evil.smudge", str(hook))
        self.write("repo/.gitattributes", "* filter=evil\n")
        copy = self.tmp / "copy"
        shutil.copytree(self.files, copy)
        vcs_files.file_status(self.config, "git", self.repo, "HEAD", self.files, copy)
        vcs_files.snapshot_commit(self.config, "git", self.repo, "HEAD", self.files, LIMITS)
        self.assertFalse(marker.exists())


@unittest.skipUnless(POSIX, "runs Python handlers")
class HandlerOutputTest(TempDirTest):
    """Whatever a handler prints is checked (VER-30)."""

    COMMIT = "a" * 40
    ID = "b" * 40

    def listing(self, output: str, code: int = 0) -> vcs_files.Listing:
        config = git_config({"list-files": fake(output, code)})
        return vcs_files.list_files(config, "git", self.tmp, "HEAD", "proj")

    def test_refused(self) -> None:
        good = f'{{"$schema": "surf-scoter-vcs-files-1", "commit": "{self.COMMIT}", "files": {{}}}}'
        self.assertEqual(self.listing(repr(good)).commit, self.COMMIT)
        entry = f'{{"kind": "file", "id": "{self.ID}", "size": 1}}'

        def with_files(files: str) -> str:
            return repr(f'{{"$schema": "surf-scoter-vcs-files-1", "commit": "{self.COMMIT}", "files": {files}}}')

        cases = {
            "not JSON": repr("nope"),
            "not a `surf-scoter-vcs-files-1` report": repr('{"$schema": "other"}'),
            "unknown keys": repr(good[:-1] + ', "extra": 1}'),
            "`commit` isn't an object id": repr(good.replace(self.COMMIT, "HEAD")),
            "not a relative path": with_files(f'{{"../x": {entry}}}'),
            "control or non-ASCII": with_files(f'{{"a\\u001b[31m": {entry}}}'),
            "must have `kind`": with_files('{"x": {"kind": "file"}}'),
            "`kind` must be": with_files(f'{{"x": {entry.replace("file", "device")}}}'),
            "`id` isn't an object id": with_files(f'{{"x": {entry.replace(self.ID, "zz")}}}'),
            "`size` must be": with_files(f'{{"x": {entry.replace(": 1", ": -1")}}}'),
            "expected int": with_files(f'{{"x": {entry.replace(": 1", ": true")}}}'),
            "expected map": with_files('{"x": []}'),
            "VCS provider 'hg' not supported": None,
            "printed more than": f"'x' * {vcs_files.MAX_REPORT_BYTES + 1}",
        }
        for message, output in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(err.ConfigErr, message):
                if output is None:
                    vcs_files.list_files(git_config({}), "hg", self.tmp, "HEAD", "proj")
                else:
                    self.listing(output)
        with self.assertRaisesRegex(vcs_files.VcsErr, "failed"):
            self.listing(repr(good), code=3)

    def test_timeout(self) -> None:
        config = git_config({"list-files": [sys.executable, "-I", "-c", "import time; time.sleep(30)"]})
        with mock.patch.object(vcs_files, "TIMEOUT_SECONDS", 0.5), \
                self.assertRaisesRegex(vcs_files.VcsErr, "stopped, or took longer"):
            vcs_files.list_files(config, "git", self.tmp, "HEAD", "proj")

    def test_values_whole_arguments(self) -> None:
        """A `{x:...}` inside a longer argument is refused, so a value is only ever itself."""
        config = git_config({"list-files": [sys.executable, "--ref={x:ref}"]})
        with self.assertRaisesRegex(err.ConfigErr, "must be a whole argument"):
            vcs_files.list_files(config, "git", self.tmp, "HEAD", "proj")

    def test_contents_bound(self) -> None:
        """`show-file` is cut off at the file limit, and must hash to the listed id."""
        listing = repr(
            f'{{"$schema": "surf-scoter-vcs-files-1", "commit": "{self.COMMIT}", '
            f'"files": {{"project.yaml": {{"kind": "file", "id": "{self.ID}", "size": 2}}}}}}'
        )
        files = self.tmp / "repo" / "files"
        files.mkdir(parents=True)
        config = git_config({"list-files": fake(listing), "show-file": fake("'ab'")})
        with self.assertRaisesRegex(vcs_files.VcsErr, "aren't the ones the listing names"):
            vcs_files.snapshot_commit(config, "git", self.tmp / "repo", "HEAD", files, LIMITS)
        config = git_config({"list-files": fake(listing), "show-file": fake("'x' * 2000")})
        with self.assertRaisesRegex(vcs_files.VcsErr, "printed more than 1000"):
            vcs_files.snapshot_commit(config, "git", self.tmp / "repo", "HEAD", files, LIMITS)

    def test_status(self) -> None:
        """Without `file-status`, everything is `unknown`; a report must name exactly the copy's files."""
        copy = self.tmp / "copy"
        self.write("copy/a", "x")
        files = self.tmp / "repo" / "files"
        files.mkdir(parents=True)
        report = vcs_files.file_status(git_config({}), "git", self.tmp / "repo", "HEAD", files, copy)
        self.assertEqual(report.files, {"a": vcs_files.FileStatus(vcs_files.Status.UNKNOWN, None)})
        other = repr(
            f'{{"$schema": "surf-scoter-vcs-file-status-1", "commit": "{self.COMMIT}", '
            f'"files": {{"b": {{"status": "committed", "id": "{self.ID}"}}}}}}'
        )
        config = git_config({"file-status": fake(other)})
        with self.assertRaisesRegex(vcs_files.VcsErr, "exactly the copy's files"):
            vcs_files.file_status(config, "git", self.tmp / "repo", "HEAD", files, copy)

    def test_repo_path(self) -> None:
        self.assertEqual(vcs_files.repo_path(self.tmp, self.tmp / "a" / "b"), "a/b")
        for outside in (self.tmp, self.tmp.parent):
            with self.subTest(outside=outside), self.assertRaises(vcs_files.VcsErr):
                vcs_files.repo_path(self.tmp, outside)
