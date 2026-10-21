#SPDX:MIT
"""Test the re-review: comparing with the earlier acceptance (step 17)."""
import datetime
import json
import sys
import unittest
from collections.abc import Callable
from pathlib import Path

# The scripts import the library as their own directory's, as they do when
# they run.
SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
import main_verify
from surf_scoter_lib import checks, review
from surf_scoter_lib.user_config import UserConfig
from surf_scoter_lib.verified import accept, lookup, records, store

from .dist_helper import HAVE_GENERATOR, NEEDS_GENERATOR, DistTest
from .test_main_verify import answers, quiet

CHANGED_PROJECT = """\
container:
  dev-container-path: ./env
egress:
  - {host: registry.npmjs.org, protocol: tls}
  - {host: github.com, protocol: tls}
commands:
  - {command: ls, args: ["-l", {any: {}}]}
  - {command: sh, args: ["-c", {any: {}}]}
"""


def first_edits(text: str) -> str:
    """The user's edits to the first review: a note, a narrower `ls`, the shell overridden."""
    text = text.replace(
        '  - {host: "registry.npmjs.org", protocol: "tls", port: 443}',
        '  - {host: "registry.npmjs.org", protocol: "tls", port: 443, $comment: "for npm"}',
    )
    text = text.replace('    args: [{any: {}}]', '    args: [{any: {prefix: "src/"}}]', 1)
    for line in ('  # - command: "sh"', '    # program: ', '    # args: ', '    # $override: '):
        text = text.replace(line, line.replace("# ", "", 1))
    return text


@unittest.skipUnless(HAVE_GENERATOR, NEEDS_GENERATOR)
class ReReviewTest(DistTest):
    """A review after an earlier acceptance shows what changed, and keeps the user's choices."""

    def review_text(self, branch: str | None = None) -> tuple[str, UserConfig, main_verify.Prompt]:
        """The review `scoter verify` writes now, and a prompt for it."""
        config = UserConfig(self.project_suc())
        taken = lookup.project_snapshot(config, self.project)
        report = checks.check_project(config)
        with quiet():
            previous = main_verify.find_previous(config, branch)
        context = review.Context(
            report=report, now=datetime.datetime.now(datetime.UTC), files=taken, branch=branch,
            previous=previous,
        )
        prompt = main_verify.Prompt(
            config, taken, branch, suggested=review.suggested(config, report),
        )
        return review.build(config, context), config, prompt

    def use(self, text: str, prompt: main_verify.Prompt, edit: Callable[[str], str] | None = None) -> None:
        """Use the review (edited with `edit`) through the prompt."""
        def editor(path: Path, _: Path) -> None:
            assert edit is not None  # nosec: B101  # narrowing for the type checker
            path.write_text(edit(path.read_text(encoding="ascii")), encoding="ascii")

        prompt.ask = answers("e", "u") if edit else answers("u")
        prompt.edit = editor if edit else None
        with quiet():
            self.assertEqual(prompt.run(text), 0)

    def test_first_review_is_full(self) -> None:
        """Without an earlier acceptance, nothing is compared."""
        text, _, _ = self.review_text()
        self.assertNotIn("compared with your acceptance", text)
        self.assertNotIn("since you accepted it", text)

    def test_re_review(self) -> None:
        """What changed is shown in full; what didn't is one line; the user's choices are kept."""
        text, _, prompt = self.review_text("main")
        self.use(text, prompt, first_edits)
        (self.files / "project.yaml").write_text(CHANGED_PROJECT, encoding="utf-8")
        (self.files / "env" / "Dockerfile").write_text("FROM scratch\nLABEL x=y\n", encoding="utf-8")

        text, _, prompt = self.review_text("main")
        lines = text.splitlines()
        self.assertTrue(any("compared with your acceptance" in line for line in lines), text)
        # Unchanged, with the user's note.
        egress = lines.index('  - {host: "registry.npmjs.org", protocol: "tls", port: 443, $comment: "for npm"}')
        self.assertEqual(lines[egress - 1], "  #scoter: unchanged since you accepted it.")
        # New.
        github = lines.index('  - {host: "github.com", protocol: "tls", port: 443}')
        self.assertIn("new: the project added this", "\n".join(lines[egress:github]))
        # Changed since: the project's setting, with the user's earlier edit commented out.
        self.assertIn("  #scoter: changed since you accepted it:", lines)
        self.assertIn('  #scoter:   args: [{any: {}}] -> ["-l", {any: {}}]', lines)
        self.assertIn('    args: ["-l", {any: {}}]', lines)
        self.assertIn("  #scoter: your version before, commented out:", lines)
        self.assertIn('    # args: [{any: {prefix: "src/"}}]', lines)
        # The shell: refused by the setup, but the user's override is kept, so it isn't commented out.
        self.assertIn('  - command: "sh"', lines)
        self.assertIn('    $override: ["host-bridge:shells"]', lines)
        self.assertTrue(any("you overrode host-bridge:shells here" in line for line in lines))
        # The Dockerfile's change.
        self.assertIn("  #scoter:   env/Dockerfile  ", text)
        self.assertIn("  #scoter:     | +LABEL x=y", lines)
        self.assertIn("  #scoter:   env/dev-env.yaml  unchanged since you accepted it", lines)
        self.assertIn("#scoter: Since you accepted it: 1 changed since, 1 new, 1 container files changed.", lines)

        # It can be used as it stands; what's stored is the shown data.
        self.use(text, prompt)
        found = lookup.find(UserConfig(self.project_suc()))
        assert found.accepted_settings is not None  # nosec: B101  # narrowing for the type checker
        accepted = json.loads(found.accepted_settings.read_text(encoding="utf-8"))
        self.assertEqual(
            [(c["command"], c["args"]) for c in accepted["commands"]],
            [("ls", ["-l", {"any": {}}]), ("sh", ["-c", {"any": {}}])],
        )
        self.assertEqual(accepted["overrides"]["commands"], [{"index": 1, "checks": ["host-bridge:shells"]}])
        self.assertEqual([r["host"] for r in accepted["egress"]], ["registry.npmjs.org", "github.com"])

    def test_nothing_changed(self) -> None:
        """The same files again: everything is one line, and the user's edits are kept."""
        text, _, prompt = self.review_text()
        self.use(text, prompt, first_edits)
        text, _, _ = self.review_text()
        self.assertIn("#scoter: Since you accepted it: 1 your changes kept.", text.splitlines())
        self.assertIn('    args: [{any: {prefix: "src/"}}]', text.splitlines())

    def test_removed_by_the_project(self) -> None:
        """An item the project dropped is offered back, commented out, with the user's note."""
        text, _, prompt = self.review_text()
        self.use(text, prompt, first_edits)
        (self.files / "project.yaml").write_text(
            "container:\n  dev-container-path: ./env\ncommands: []\n", encoding="utf-8",
        )
        lines = self.review_text()[0].splitlines()
        self.assertIn(
            '  # - {host: "registry.npmjs.org", protocol: "tls", port: 443, $comment: "for npm"}', lines,
        )
        self.assertIn(
            "  #scoter: the project removed this since you accepted it; remove the `# ` to keep it anyway.",
            lines,
        )
        # Its note is listed in the header too, so it isn't lost unnoticed.
        self.assertTrue(any(
            line.startswith("#scoter:   your note on an item the project removed (egress;") and line.endswith("for npm")
            for line in lines
        ), lines)

    def test_base_prefers_the_branch_and_takes_expired(self) -> None:
        """The newest on the branch, though there's a newer one elsewhere, and though it has expired."""
        text, _, prompt = self.review_text("main")
        self.use(text, prompt)
        (self.files / "env" / "Dockerfile").write_text("FROM scratch\nLABEL other=1\n", encoding="utf-8")
        text, _, prompt = self.review_text("other")
        self.use(text, prompt)
        env_id = store.environment_id(self.project)
        verified_store = store.VerifiedStore.open(self.verified)
        record = records.load_environment(verified_store, str(self.project), env_id)
        assert record is not None  # nosec: B101  # narrowing for the type checker
        on_main = next(a for a in record.acceptances if a.branch == "main")
        # Expire it: it expires as it's accepted.
        expired = on_main.accepted_at
        record.acceptances = [
            a if a.id != on_main.id else records.Acceptance(**{**a.__dict__, "expires_at": expired})
            for a in record.acceptances
        ]
        with verified_store.locked():
            records.save_environment(verified_store, record)
        config = UserConfig(self.project_suc())
        with quiet():
            previous = main_verify.find_previous(config, "main")
        assert previous is not None  # nosec: B101  # narrowing for the type checker
        self.assertEqual(previous.acceptance.id, on_main.id)
        self.assertEqual(previous.files["env/Dockerfile"], b"FROM scratch\n")
        text = self.review_text("main")[0]
        self.assertIn(f"compared with your acceptance {on_main.id} (branch main, expired", text)
        # The refused shell the user didn't take isn't counted as their removal.
        self.assertIn("#scoter: Since you accepted it: 1 container files changed.", text.splitlines())

    def test_stores_the_suggestion(self) -> None:
        """An acceptance keeps the suggestion as the review wrote it, for the next review."""
        text, _, prompt = self.review_text()
        self.use(text, prompt)
        found = lookup.find(UserConfig(self.project_suc()))
        assert found.accepted_settings is not None  # nosec: B101  # narrowing for the type checker
        stored = json.loads((found.accepted_settings.parent / accept.SUGGESTION_FILE).read_text())
        self.assertEqual(
            [c["command"] for c in stored["review"]["commands"]], ["ls", "sh"],
        )


if __name__ == "__main__":
    unittest.main()
