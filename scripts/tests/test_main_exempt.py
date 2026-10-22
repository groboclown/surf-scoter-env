#SPDX:MIT
"""Test the main_exempt script, and the exempt library it uses."""
import contextlib
import datetime
import io
import sys
import unittest
from pathlib import Path

# The scripts import the library as their own directory's, as they do when
# they run.
SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
import main_exempt
from surf_scoter_lib.user_config import UserConfig
from surf_scoter_lib.verified import exempt, records, store

from .test_main_revoke import POSIX, StoreTest

DAY = datetime.timedelta(days=1)


class ParseDurationTest(unittest.TestCase):
    """Test reading `--for`."""

    def test_lengths(self) -> None:
        """Days, hours and minutes, alone or summed; anything else isn't a length."""
        for text, length in (
            ("1d", DAY),
            ("8h", datetime.timedelta(hours=8)),
            ("30m", datetime.timedelta(minutes=30)),
            ("1d12h", datetime.timedelta(days=1, hours=12)),
        ):
            with self.subTest(text=text):
                self.assertEqual(exempt.parse_duration(text), length)
        for text in ("", "1", "d", "1x", "1d ", "0d", "h1", "1d-2h"):
            with self.subTest(text=text), self.assertRaises(exempt.ExemptErr):
                exempt.parse_duration(text)


@unittest.skipUnless(POSIX, "owner and mode checks")
class ExemptTest(StoreTest):
    """Starting and ending an exemption."""

    def with_values(self, config: UserConfig, **values: int) -> UserConfig:
        """`config`, with `verification: values` set."""
        suc = config.raw()
        assert isinstance(suc, dict)  # nosec: B101  # narrowing for the type checker
        suc["resolved"]["verification"]["values"] = {  # type: ignore[index]
            key.replace("_", "-"): value for key, value in values.items()
        }
        return UserConfig(suc)

    def record(self) -> records.EnvironmentRecord:
        found = records.load_environment(
            store.VerifiedStore.open(self.verified), str(self.project), store.environment_id(self.project),
        )
        assert found is not None  # nosec: B101  # narrowing for the type checker
        return found

    def run_exempt(self, length: str | None, *, end: bool = False) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            action = main_exempt.Action.END if end else main_exempt.Action.START
            code = main_exempt.run_exempt(self.config(branch="main"), length, action=action)
        return code, out.getvalue(), err.getvalue()

    def test_nothing_to_exempt(self) -> None:
        """Without a usable acceptance, there's nothing to start from."""
        code, _, err = self.run_exempt(None)
        self.assertEqual(code, 1)
        self.assertIn("run `scoter verify` first", err)
        accepted = self.accept("FROM scratch\n", "main")
        later = accepted.expires_at + DAY
        with self.assertRaisesRegex(exempt.ExemptErr, "no usable acceptance"):
            exempt.start(self.config(), later)

    def test_start(self) -> None:
        """From the newest usable acceptance, for the branch; the default and longest lengths."""
        self.accept("FROM scratch\n", "main")
        newest = self.accept("FROM alpine\n", "main")
        now = records.now_utc()
        started = exempt.start(self.config(branch="main"), now)
        self.assertEqual(
            started, records.Exemption(newest.id, now + DAY * exempt.DEFAULT_DAYS, "main", now),
        )
        self.assertEqual(self.record().exemption, started)
        self.assertEqual(exempt.current(self.config(branch="main"), now), started)
        self.assertIsNone(exempt.current(self.config(branch="dev"), now))
        self.assertIsNone(exempt.current(self.config(branch="main"), started.until))
        # The setup's lengths; never past the acceptance's expiry (three days).
        config = self.with_values(self.config(branch="main"), exempt_days=2, max_exempt_days=5)
        self.assertEqual(exempt.start(config, now).until, now + 2 * DAY)
        self.assertEqual(exempt.start(config, now, 4 * DAY).until, newest.expires_at)
        with self.assertRaisesRegex(exempt.ExemptErr, "at most 5 days"):
            exempt.start(config, now, 6 * DAY)

    def test_branchs_acceptance_first(self) -> None:
        """The branch's acceptance, though another branch's is newer; else the newest."""
        mine = self.accept("FROM scratch\n", "main")
        newer = self.accept("FROM alpine\n", "dev")
        now = records.now_utc()
        self.assertEqual(exempt.start(self.config(branch="main"), now).acceptance, mine.id)
        self.assertEqual(exempt.start(self.config(branch="other"), now).acceptance, newer.id)

    def test_command(self) -> None:
        """`scoter exempt --for`, then `--end`."""
        accepted = self.accept("FROM scratch\n", "main")
        code, out, _ = self.run_exempt("8h")
        self.assertEqual(code, 0)
        self.assertIn("Exempt on main until ", out)
        self.assertIn(f"from your acceptance {accepted.id}", out)
        exemption = self.record().exemption
        assert exemption is not None  # nosec: B101  # narrowing for the type checker
        self.assertEqual(exemption.until - exemption.started_at, datetime.timedelta(hours=8))
        self.assertEqual(self.run_exempt("8 hours")[0], 1)
        self.assertEqual(self.run_exempt("8d")[0], 1)
        self.assertEqual(self.run_exempt(None, end=True), (0, "The exemption ended.\n", ""))
        self.assertIsNone(self.record().exemption)
        self.assertEqual(self.run_exempt(None, end=True), (0, "There's no exemption to end.\n", ""))
