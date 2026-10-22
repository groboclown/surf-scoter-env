#SPDX:MIT
"""Test each environment's ssh entries (ticket #12)."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from ...surf_scoter_lib import fileutil, ssh_hosts
from ...surf_scoter_lib.err import ConfigErr
from ...surf_scoter_lib.user_config import UserConfig

# Records its arguments in the file named by its first argument.
RECORDER = "import json, sys; open(sys.argv[1], 'a').write(json.dumps(sys.argv[2:]) + '\\n')"


class _Terminal(io.StringIO):
    """Input that says it's a terminal, and fails the test if it's read."""

    def isatty(self) -> bool:
        return True

    def readline(self, size: int | None = -1) -> str:
        raise AssertionError("asked, with the output going to a file")


class SshHostsTest(unittest.TestCase):
    """Writing, finding and removing the environments' `Host` blocks."""

    def setUp(self) -> None:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.tmp = Path(os.path.realpath(directory))
        (self.tmp / "key").write_text("", encoding="utf-8")
        self.file = self.tmp / "config"
        self.file.write_text("Host mine\n  User me\n", encoding="utf-8")
        self.record = self.tmp / "record"

    def config(self, *, project_id: object = "my-project-c593c7ec2d55", prefix: str = "scoter-",
               mode: object = None) -> UserConfig:
        values: dict[str, object] = {"host-prefix": prefix}
        if mode is not None:
            values["config-cleanup"] = mode
        recorder = [sys.executable, "-c", RECORDER, str(self.record)]
        return UserConfig({
            "active-config-locations": {"project-id": project_id},
            "resolved": {
                "scoter": {"values": {"backup-file-extension": ".backup"}},
                "ssh": {
                    "paths": {"key-file": str(self.tmp / "key"), "config-file": str(self.file)},
                    "values": values,
                    "runs": {
                        "remove-known-host": [*recorder, "forget", "{x:host}"],
                        "proxy-command": ["docker", "exec", "-i", "%h", "socat", "STDIO", "TCP:127.0.0.1:{x:port}"],
                    },
                },
            },
        })

    def forgotten(self) -> list[list[str]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def test_host_name(self) -> None:
        """The prefix and the project id; a name that can't be a container's or a `Host` is refused."""
        self.assertEqual(ssh_hosts.host_name(self.config()), "scoter-my-project-c593c7ec2d55")
        for config in (self.config(prefix="Bad Prefix "), self.config(prefix="x*"), self.config(project_id=None)):
            with self.subTest(), self.assertRaises(ConfigErr):
                ssh_hosts.host_name(config)

    def test_cleanup_mode(self) -> None:
        """`ask` by default; a bare YAML `no` (false) is `no`; anything else is refused."""
        for given, mode in ((None, "ask"), ("ask", "ask"), ("auto", "auto"), ("no", "no"), (False, "no")):
            with self.subTest(given=given):
                self.assertEqual(ssh_hosts.cleanup_mode(self.config(mode=given)), mode)
        with self.assertRaisesRegex(ConfigErr, "must be `no`, `auto` or `ask`"):
            ssh_hosts.cleanup_mode(self.config(mode="sometimes"))

    def test_write_and_replace(self) -> None:
        """Its own block, marked; written again in place of the old one; the user's left alone."""
        config = self.config()
        ssh_hosts.write_host(config, "scoter-p", "ubuntu", "20000")
        ssh_hosts.write_host(config, "scoter-p", "ubuntu", "20001")
        text = self.file.read_text(encoding="utf-8")
        self.assertEqual(text.count("Host scoter-p\n"), 1)
        self.assertIn(f"  {ssh_hosts.MARKER}", text)
        self.assertIn("  ProxyCommand docker exec -i %h socat STDIO TCP:127.0.0.1:20001\n", text)
        self.assertNotIn("20000", text)
        self.assertTrue(text.startswith("Host mine\n  User me\n"))
        # Written again and again, the file doesn't grow.
        ssh_hosts.write_host(config, "scoter-p", "ubuntu", "20001")
        self.assertEqual(self.file.read_text(encoding="utf-8"), text)
        if not fileutil.is_windows():
            self.assertEqual(os.stat(self.file).st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(ConfigErr, "has a `Host mine` the tools didn't write"):
            ssh_hosts.write_host(config, "mine", "ubuntu", "20000")
        # Nothing from the project that could add a line (DEV-1).
        for user, port in (("ubuntu\nMatch all", "20000"), ("a b", "20000"), ("", "20000"), ("ubuntu", "1 2")):
            with self.subTest(user=user, port=port), self.assertRaises(ConfigErr):
                ssh_hosts.write_host(config, "scoter-q", user, port)
        self.assertNotIn("scoter-q", self.file.read_text(encoding="utf-8"))

    def test_leftovers_and_removing(self) -> None:
        """Only the tools' blocks with the prefix, not running; removing takes the known host key too."""
        config = self.config()
        for host in ("scoter-a", "scoter-b"):
            ssh_hosts.write_host(config, host, "ubuntu", "20000")
        with open(self.file, "a", encoding="utf-8") as out:
            out.write("Host scoter-users-own\n  User me\n")
        self.assertEqual(ssh_hosts.leftover_hosts(config, ["scoter-a"]), ["scoter-b"])
        self.assertEqual(ssh_hosts.remove_hosts(config, ["scoter-b", "mine", "scoter-users-own"]), ["scoter-b"])
        text = self.file.read_text(encoding="utf-8")
        self.assertNotIn("Host scoter-b", text)
        self.assertIn("Host scoter-a", text)
        self.assertIn("Host scoter-users-own", text)
        self.assertEqual(self.forgotten(), [["forget", "scoter-b"]])

    def test_release(self) -> None:
        """`no` keeps; `auto` removes; `ask` asks, and keeps without a terminal."""
        ssh_hosts.write_host(self.config(), "scoter-a", "ubuntu", "20000")
        self.assertFalse(ssh_hosts.release(self.config(mode="no"), "scoter-a"))
        self.assertFalse(ssh_hosts.release(self.config(mode="ask"), "scoter-a", ask=lambda _: "n"))
        stdin, sys.stdin = sys.stdin, io.StringIO()
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertFalse(ssh_hosts.release(self.config(), "scoter-a"))
        finally:
            sys.stdin = stdin
        self.assertIn("`scoter cleanup` can remove them later", err.getvalue())
        # A terminal to type at, but the output going to a file: the question
        # wouldn't be seen, so it isn't asked.
        stdin, sys.stdin = sys.stdin, _Terminal()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(ssh_hosts.release(self.config(), "scoter-a"))
        finally:
            sys.stdin = stdin
        self.assertIn("Host scoter-a", self.file.read_text(encoding="utf-8"))
        self.assertTrue(ssh_hosts.release(self.config(mode="ask"), "scoter-a", ask=lambda _: "y"))
        ssh_hosts.write_host(self.config(), "scoter-a", "ubuntu", "20000")
        self.assertTrue(ssh_hosts.release(self.config(mode="auto"), "scoter-a"))
        self.assertNotIn("Host scoter-a", self.file.read_text(encoding="utf-8"))
