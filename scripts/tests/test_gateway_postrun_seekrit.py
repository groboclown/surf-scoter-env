#SPDX:MIT
"""Test the gateway_postrun_seekrit script, and starting the gateway (its CA and post-start)."""
import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
import gateway_postrun_seekrit as postrun
from surf_scoter_lib import UserConfig, fileutil, gateway_start

POSIX = not fileutil.is_windows()

# A stand-in for `docker exec -i CONTAINER ...`: it records its arguments and
# standard input, and exits with the next code in its list (then 0).
FAKE_EXEC = """\
import json, sys
record, codes = sys.argv[1], sys.argv[2]
with open(codes) as f:
    left = json.load(f)
code = left.pop(0) if left else 0
with open(codes, "w") as f:
    json.dump(left, f)
with open(record, "a") as f:
    f.write(json.dumps({"args": sys.argv[3:], "stdin": sys.stdin.read()}) + "\\n")
sys.exit(code)
"""

# A stand-in for `docker inspect --format json CONTAINER`: it prints the next
# container state in its list (the last one again, once it's the only one
# left); `null` is a container it can't inspect yet.
FAKE_INSPECT = """\
import json, sys
states = sys.argv[1]
with open(states) as f:
    left = json.load(f)
state = left.pop(0) if len(left) > 1 else left[0]
with open(states, "w") as f:
    json.dump(left, f)
if state is None:
    sys.exit(125)
print(json.dumps([{"State": state}]))
"""


def _state(status: str = "running", age_seconds: float = 1.0) -> dict[str, object]:
    """A container state, as Podman writes it: started `age_seconds` ago."""
    started = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=age_seconds)
    local = started.astimezone().isoformat(timespec="microseconds")
    # Nanoseconds, and the local offset (`-06:00`).
    return {"Status": status, "Running": status == "running", "StartedAt": f"{local[:-6]}123{local[-6:]}"}


def _rule(host: str, protocol: str = "https", **extra: object) -> dict[str, object]:
    return {"host": host, "protocol": protocol, **extra}


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.project = self.dir / "project"
        self.project.mkdir()
        self.store = self.dir / "home" / "secrets" / "p.toml"
        self.store.parent.mkdir(parents=True)
        self.record = self.dir / "exec.jsonl"
        self.codes = self.dir / "codes.json"
        self.codes.write_text("[]", encoding="utf-8")
        fake = self.dir / "fake_exec.py"
        fake.write_text(FAKE_EXEC, encoding="utf-8")
        self.rules: list[dict[str, object]] = []
        self.exec_stdin = [sys.executable, str(fake), str(self.record), str(self.codes), "{x:container}"]
        self.states = self.dir / "states.json"
        self.set_states(_state())
        inspect = self.dir / "fake_inspect.py"
        inspect.write_text(FAKE_INSPECT, encoding="utf-8")
        self.inspect = [sys.executable, str(inspect), str(self.states)]
        self.start_seconds: object = 300

    def set_states(self, *states: dict[str, object] | None) -> None:
        self.states.write_text(json.dumps(list(states)), encoding="utf-8")

    def config(self) -> UserConfig:
        return UserConfig({
            "active-config-locations": {"project-dir": str(self.project)},
            "egress": {"rules": self.rules},
            "resolved": {
                "project-state": {"paths": {
                    "secret-store": str(self.store),
                    "project-state-dir": str(self.dir / "state"),
                }},
                "docker": {"runs": {"exec-stdin": self.exec_stdin, "inspect-container": self.inspect}},
                "dev-env": {"values": {"network-gateway-start-seconds": self.start_seconds}},
            },
        })

    def write_store(self, text: str, mode: int = 0o600) -> None:
        self.store.write_text(text, encoding="utf-8")
        os.chmod(self.store, mode)

    def sent(self) -> list[dict[str, object]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]


class PayloadTest(_Base):
    """Test what the gateway is sent."""

    def test_names_in_order_once(self) -> None:
        """The rules' secret names, each once."""
        self.rules = [
            _rule("a.example.com", secrets=["B", "A"]),
            _rule("b.example.com", secrets=["A", "C"]),
            _rule("c.example.com", "tls"),
        ]
        self.assertEqual(postrun.secret_names(self.config()), ["B", "A", "C"])

    def test_no_names_needs_no_store(self) -> None:
        """No secrets named: an empty set, with no store at all."""
        self.assertEqual(json.loads(postrun.payload(self.config(), [])), {"values": {}})

    @unittest.skipUnless(POSIX, "file modes")
    def test_values(self) -> None:
        """Only the named values, from literals and commands; a command's line ending dropped."""
        self.write_store(
            "[secrets]\n"
            'A = { value = "literal" }\n'
            f'B = {{ command = [{json.dumps(sys.executable)}, "-c", "print(\\"from-command\\")"] }}\n'
            'UNUSED = { value = "never sent" }\n'
        )
        data = json.loads(postrun.payload(self.config(), ["A", "B"]))
        self.assertEqual(data, {"values": {"A": "literal", "B": "from-command"}})

    @unittest.skipUnless(POSIX, "file modes")
    def test_token(self) -> None:
        """A seekrit.dev token instead of values."""
        self.write_store('seekrit-token = { value = "skt_abc" }\n')
        self.assertEqual(json.loads(postrun.payload(self.config(), ["A"])), {"token": "skt_abc"})  # nosec: B105  # A test value.

    @unittest.skipUnless(POSIX, "file modes")
    def test_refused(self) -> None:
        """Every problem stops it before anything is sent, and no message quotes a value."""
        cases = [
            ("[secrets]\nA = { value = \"x\" }\n", 0o644, "make it readable only by you"),
            ("[secrets]\nA = { value = \"x\" }\n", 0o640, "make it readable only by you"),
            ("[secrets]\nB = { value = \"s3cr3t\" }\n", 0o600, "doesn't have"),
            ('seekrit-token = { value = "t" }\n[secrets]\nA = { value = "x" }\n', 0o600, "give one"),
            ("other = 1\n", 0o600, "can only have"),
            ("[secrets]\nA = \"s3cr3t\"\n", 0o600, "must be `{ value"),
            ("[secrets]\nA = { value = \"x\", command = [\"echo\"] }\n", 0o600, "must be `{ value"),
            ("[secrets]\nA = { value = \"\" }\n", 0o600, "is empty"),
            ("[secrets]\nA = { command = [] }\n", 0o600, "list of text"),
            ("[secrets]\nA = { command = [\"no-such-program-xyz\"] }\n", 0o600, "can't find"),
            ("[secrets]\nA = { command = [\"bin/x\"] }\n", 0o600, "a name or an absolute path"),
            (f"[secrets]\nA = {{ command = [{json.dumps(sys.executable)}, \"-c\", \"exit(3)\"] }}\n",
             0o600, "failed (exit 3)"),
            ("[secrets\nA = s3cr3t", 0o600, "isn't TOML"),
        ]
        for text, mode, expect in cases:
            with self.subTest(text=text, mode=oct(mode)):
                self.write_store(text, mode)
                with self.assertRaises(postrun.StoreErr) as caught:
                    postrun.payload(self.config(), ["A"])
                self.assertIn(expect, str(caught.exception))
                self.assertNotIn("s3cr3t", str(caught.exception))

    def test_no_store(self) -> None:
        """A name, but no store, or no store setting."""
        with self.assertRaisesRegex(postrun.StoreErr, "no secret store"):
            postrun.payload(self.config(), ["A"])
        config = self.config()
        config.raw()["resolved"]["project-state"]["paths"]["secret-store"] = None  # type: ignore[index]
        with self.assertRaisesRegex(postrun.StoreErr, "has no `project-state: paths: secret-store`"):
            postrun.payload(config, ["A"])

    @unittest.skipUnless(POSIX, "symlinks")
    def test_store_place(self) -> None:
        """Not a symlink, and not in the project, where it could have written it."""
        real = self.dir / "real.toml"
        real.write_text("[secrets]\nA = { value = \"x\" }\n", encoding="utf-8")
        os.chmod(real, 0o600)
        self.store.symlink_to(real)
        with self.assertRaisesRegex(postrun.StoreErr, "symlink"):
            postrun.payload(self.config(), ["A"])
        self.store.unlink()
        inside = self.project / "store.toml"
        inside.write_text("[secrets]\nA = { value = \"x\" }\n", encoding="utf-8")
        os.chmod(inside, 0o600)
        config = self.config()
        config.raw()["resolved"]["project-state"]["paths"]["secret-store"] = str(inside)  # type: ignore[index]
        with self.assertRaisesRegex(postrun.StoreErr, "the project directory"):
            postrun.payload(config, ["A"])

    @unittest.skipUnless(POSIX, "file modes")
    def test_program_in_the_project(self) -> None:
        """A store command whose program is in the project would run the project's code."""
        program = self.project / "helper"
        program.write_text("#!/bin/sh\necho x\n", encoding="utf-8")
        os.chmod(program, 0o755)  # nosec: B103  # A test program, in a test directory.
        self.write_store(f"[secrets]\nA = {{ command = [{json.dumps(str(program))}] }}\n")
        with self.assertRaisesRegex(postrun.StoreErr, "would run the project's code"):
            postrun.payload(self.config(), ["A"])


class ScriptTest(_Base):
    """Test the whole script, with a stand-in for `docker exec`."""

    def run_main(self) -> int:
        universal = self.dir / "suc.json"
        universal.write_text(json.dumps(self.config().raw()), encoding="utf-8")
        with mock.patch.object(postrun, "POLL_SECONDS", 0):
            return postrun.main(["gateway_postrun_seekrit.py", str(universal), "proj-ng"])

    @unittest.skipUnless(POSIX, "file modes")
    def test_sends_on_stdin(self) -> None:
        """The values go on the container's standard input, never on its command line."""
        self.rules = [_rule("api.github.com", secrets=["GITHUB_TOKEN"])]
        self.write_store('[secrets]\nGITHUB_TOKEN = { value = "s3cr3t" }\n')
        self.assertEqual(self.run_main(), 0)
        sent = self.sent()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["args"], ["proj-ng", *postrun.INJECT])
        self.assertEqual(json.loads(sent[0]["stdin"]), {"values": {"GITHUB_TOKEN": "s3cr3t"}})  # type: ignore[arg-type]  # nosec: B105  # A test value.
        self.assertNotIn("s3cr3t", json.dumps(sent[0]["args"]))

    def test_waits_for_the_container(self) -> None:
        """Not there to inspect, then created, then running: only then is anything sent."""
        self.set_states(None, None, _state("created"), _state("running"))
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.sent()), 1)

    def test_waits_for_commands_and_the_socket(self) -> None:
        """Running, but not taking commands (125), then no socket yet (2): tried again."""
        self.codes.write_text("[125, 2, 2, 0]", encoding="utf-8")
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.sent()), 4)

    def test_refused_isnt_retried(self) -> None:
        """The gateway refused the secrets (1): no point sending them again."""
        self.codes.write_text("[1]", encoding="utf-8")
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(len(self.sent()), 1)

    def test_already_delivered(self) -> None:
        """Long started, and no socket: it already has its secrets, so it stops at once."""
        self.set_states(_state("running", age_seconds=postrun.SOCKET_WINDOW_SECONDS + 600))
        self.codes.write_text("[2, 2, 2]", encoding="utf-8")
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(len(self.sent()), 1)

    def test_exited(self) -> None:
        """A gateway that stopped: nothing is sent."""
        self.set_states(_state("exited"))
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sent(), [])

    def test_gives_up(self) -> None:
        """A gateway that never runs: it stops at the setup's time."""
        self.start_seconds = 0.05
        self.set_states(None)
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sent(), [])

    def test_started_at(self) -> None:
        """Docker's and Podman's start times, to the nanosecond; nothing else."""
        utc = datetime.UTC
        self.assertEqual(
            postrun.started_at("2026-11-08T02:08:33.123456789Z"),
            datetime.datetime(2026, 11, 8, 2, 8, 33, 123456, tzinfo=utc),
        )
        self.assertEqual(
            postrun.started_at("2026-11-07T23:49:00.556699938-06:00"),
            datetime.datetime(2026, 11, 8, 5, 49, 0, 556699, tzinfo=utc),
        )
        self.assertIsNone(postrun.started_at("0001-01-01"))
        self.assertIsNone(postrun.started_at("soon"))

    def test_start_seconds(self) -> None:
        """The setup's, or the default; a value that isn't a number is an error."""
        self.assertEqual(postrun.start_seconds(self.config()), 300)
        self.start_seconds = None
        self.assertEqual(postrun.start_seconds(self.config()), postrun.START_SECONDS)
        self.start_seconds = "soon"
        with self.assertRaises(postrun.SurfScoterErr):
            postrun.start_seconds(self.config())

    def test_store_problem_sends_nothing(self) -> None:
        """A store that can't give a name: nothing is sent."""
        self.rules = [_rule("api.github.com", secrets=["GITHUB_TOKEN"])]
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sent(), [])


class GatewayStartTest(_Base):
    """Test the run's CA, and the generic post-start."""

    @unittest.skipUnless(POSIX and shutil.which("openssl"), "openssl and file modes")
    def test_make_ca(self) -> None:
        """A new CA each time: a certificate anyone may read, a key only you can."""
        openssl = shutil.which("openssl")
        config = UserConfig({"resolved": {"dev-env": {"runs": {"network-gateway-mk-ca": [
            openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
            "-keyout", "{x:ca-key}", "-out", "{x:ca-cert}", "-days", "1", "-subj", "/CN=test",
        ]}}}})
        resources = self.dir / "resources"
        resources.mkdir()
        cert, key = gateway_start.make_ca(config, resources)
        self.assertEqual(os.stat(key).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(cert).st_mode & 0o777, 0o644)
        self.assertEqual(os.stat(cert.parent).st_mode & 0o777, 0o700)
        first = key.read_bytes()
        cert, key = gateway_start.make_ca(config, resources)
        self.assertNotEqual(key.read_bytes(), first, "never reused")
        gateway_start.remove_ca(resources)
        self.assertFalse(cert.parent.exists())

    @unittest.skipUnless(POSIX and shutil.which("openssl"), "openssl and file modes")
    def test_make_shared_ca(self) -> None:
        """A gateway that reads TLS reads the key: readable by others, inside a private directory."""
        openssl = shutil.which("openssl")
        config = UserConfig({"resolved": {"dev-env": {"runs": {"network-gateway-mk-ca": [
            openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
            "-keyout", "{x:ca-key}", "-out", "{x:ca-cert}", "-days", "1", "-subj", "/CN=test",
        ]}}}})
        private = self.dir / "state"
        private.mkdir(mode=0o700)
        os.chmod(private, 0o700)
        resources = private / "resources"
        resources.mkdir(mode=0o755)
        cert, key = gateway_start.make_ca(config, resources, shared=True)
        self.assertEqual(os.stat(key).st_mode & 0o777, 0o644)
        self.assertEqual(os.stat(cert.parent).st_mode & 0o777, 0o755)
        self.assertEqual(gateway_start.private_ancestor(cert.parent), private)
        self.assertEqual(gateway_start.gateway_ca_dir(resources, True), cert.parent)
        # Nothing above it private: refused, and nothing made.
        os.chmod(private, 0o755)  # nosec: B103  # A test directory, made not private on purpose.
        if gateway_start.private_ancestor(resources / "ca") is None:
            with self.assertRaisesRegex(Exception, "only you can use"):
                gateway_start.make_ca(config, resources, shared=True)

    def test_no_ca_for_a_gateway_that_doesnt_read_tls(self) -> None:
        """Squid gets an empty directory, never the key."""
        resources = self.dir / "resources"
        resources.mkdir()
        (resources / "gateway-no-ca").mkdir()
        (resources / "gateway-no-ca" / "left.pem").write_text("x", encoding="utf-8")
        empty = gateway_start.gateway_ca_dir(resources, False)
        self.assertEqual(empty, resources / "gateway-no-ca")
        self.assertEqual(list(empty.iterdir()), [])

    def test_reads_tls(self) -> None:
        """The setup's yes or no; unset is no; anything else, an error."""
        def config(value: object) -> UserConfig:
            return UserConfig({"resolved": {"dev-env": {"values": {"network-gateway-reads-tls": value}}}})
        self.assertTrue(gateway_start.reads_tls(config("yes")))
        self.assertTrue(gateway_start.reads_tls(config("True")))
        self.assertFalse(gateway_start.reads_tls(config("no")))
        self.assertFalse(gateway_start.reads_tls(UserConfig({"resolved": {}})))
        with self.assertRaises(gateway_start.ConfigErr):
            gateway_start.reads_tls(config("maybe"))

    def test_make_ca_fails(self) -> None:
        """A command that fails: an error, not a run without a CA."""
        config = UserConfig({"resolved": {"dev-env": {"runs": {"network-gateway-mk-ca": [
            sys.executable, "-c", "import sys; sys.exit(1)",
        ]}}}})
        with self.assertRaisesRegex(Exception, "Couldn't make the network gateway's CA"):
            gateway_start.make_ca(config, self.dir)

    def test_post_start(self) -> None:
        """No post-start set: nothing to do.  Set: it runs, and its exit code counts."""
        self.assertTrue(gateway_start.post_start(UserConfig({"resolved": {}}), self.dir / "u.json", "c"))
        for code, worked in ((0, True), (1, False)):
            config = UserConfig({"resolved": {"dev-env": {"runs": {"network-gateway-post-start": [
                sys.executable, "-c", f"import sys; sys.exit({code} if sys.argv[1:] == ['u', 'c'] else 9)",
                "{x:universal}", "{x:gateway-container}",
            ]}}}})
            with self.subTest(code=code):
                self.assertEqual(gateway_start.post_start(config, Path("u"), "c"), worked)
