#SPDX:MIT
"""Test the gateway_postrun_envoy script: how it gives the Envoy gateway its secrets.

The delivery itself is `surf_scoter_lib.gateway_secrets`'s (`test_gateway_secrets`).
"""
import json
import unittest
from unittest import mock

import gateway_envoy
import gateway_postrun_envoy
from surf_scoter_lib import gateway_secrets

from .test_gateway_secrets import POSIX, _Base, _rule


class EnvoyScriptTest(_Base):
    """Test the whole script, with a stand-in for `docker exec`."""

    def run_main(self) -> int:
        universal = self.dir / "suc.json"
        universal.write_text(json.dumps(self.config().raw()), encoding="utf-8")
        with mock.patch.object(gateway_secrets, "POLL_SECONDS", 0):
            return gateway_postrun_envoy.main(["gateway_postrun_envoy.py", str(universal), "proj-ng"])

    def test_injector(self) -> None:
        """Envoy takes values only, keeps its socket, and has none when the rules name no secrets."""
        self.assertEqual(gateway_postrun_envoy.INJECTOR, gateway_secrets.Injector(
            command=("/usr/local/bin/envoy-inject", "--socket", gateway_envoy.INJECT_SOCKET),
            allow_token=False, socket_closes=False, skip_without_names=True,
        ))

    @unittest.skipUnless(POSIX, "file modes")
    def test_sends_on_stdin(self) -> None:
        """The values go to `envoy-inject` on the container's standard input, never on its command line."""
        self.rules = [_rule("api.github.com", secrets=["GITHUB_TOKEN"])]
        self.write_store('[secrets]\nGITHUB_TOKEN = { value = "s3cr3t" }\n')
        self.assertEqual(self.run_main(), 0)
        sent = self.sent()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["args"], ["proj-ng", *gateway_postrun_envoy.INJECTOR.command])
        self.assertEqual(json.loads(sent[0]["stdin"]), {"values": {"GITHUB_TOKEN": "s3cr3t"}})  # type: ignore[arg-type]  # nosec: B105  # A test value.
        self.assertNotIn("s3cr3t", json.dumps(sent[0]["args"]))

    @unittest.skipUnless(POSIX, "file modes")
    def test_no_token(self) -> None:
        """Envoy can't resolve a seekrit.dev token: a store with one sends nothing."""
        self.rules = [_rule("api.github.com", secrets=["GITHUB_TOKEN"])]
        self.write_store('seekrit-token = { value = "skt_abc" }\n')
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.sent(), [])

    def test_no_secrets_does_nothing(self) -> None:
        """Rules without secrets: the gateway has no inject socket, so nothing is sent."""
        self.rules = [_rule("api.github.com"), _rule("registry.npmjs.org", "tls")]
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.sent(), [])
