#SPDX:MIT
"""Test the gateway_postrun_seekrit script: how it gives the seekrit gateway its secrets.

The delivery itself is `surf_scoter_lib.gateway_secrets`'s (`test_gateway_secrets`).
"""
import json
import unittest
from unittest import mock

import gateway_postrun_seekrit
import gateway_seekrit
from surf_scoter_lib import gateway_secrets

from .test_gateway_secrets import POSIX, _Base, _rule, _state


class SeekritScriptTest(_Base):
    """Test the whole script, with a stand-in for `docker exec`."""

    def run_main(self) -> int:
        universal = self.dir / "suc.json"
        universal.write_text(json.dumps(self.config().raw()), encoding="utf-8")
        with mock.patch.object(gateway_secrets, "POLL_SECONDS", 0):
            return gateway_postrun_seekrit.main(["gateway_postrun_seekrit.py", str(universal), "proj-ng"])

    def test_injector(self) -> None:
        """seekrit-proxy takes a token, closes its socket after a delivery, and is sent an empty
        set when the rules name no secrets (which closes it)."""
        self.assertEqual(gateway_postrun_seekrit.INJECTOR, gateway_secrets.Injector(
            command=("/seekrit-proxy", "inject", "--socket", gateway_seekrit.INJECT_SOCKET),
            allow_token=True, socket_closes=True, skip_without_names=False,
        ))

    @unittest.skipUnless(POSIX, "file modes")
    def test_sends_on_stdin(self) -> None:
        """The token goes to `seekrit-proxy inject` on the container's standard input."""
        self.rules = [_rule("api.github.com", secrets=["GITHUB_TOKEN"])]
        self.write_store('seekrit-token = { value = "skt_abc" }\n')
        self.assertEqual(self.run_main(), 0)
        sent = self.sent()
        self.assertEqual(sent[0]["args"], ["proj-ng", *gateway_postrun_seekrit.INJECTOR.command])
        self.assertEqual(json.loads(sent[0]["stdin"]), {"token": "skt_abc"})  # type: ignore[arg-type]  # nosec: B105  # A test value.

    def test_already_delivered(self) -> None:
        """Long started, and no socket: it already has its secrets, so it stops at once."""
        self.set_states(_state("running", age_seconds=gateway_secrets.SOCKET_WINDOW_SECONDS + 600))
        self.codes.write_text("[2, 2, 2]", encoding="utf-8")
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(len(self.sent()), 1)
