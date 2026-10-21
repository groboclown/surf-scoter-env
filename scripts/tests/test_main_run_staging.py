#SPDX:MIT
"""Test staging the accepted copy of a project's files for the run (step 16)."""
import datetime
import json
import os
import sys
import unittest
from collections.abc import Callable
from pathlib import Path

# The scripts import the library as their own directory's, as they do when
# they run.
SCRIPTS = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS))
import main_verify
from surf_scoter_lib import checks, generator, review
from surf_scoter_lib.user_config import UserConfig
from surf_scoter_lib.verified import lookup, staging

from .dist_helper import HAVE_GENERATOR, NEEDS_GENERATOR, DistTest
from .test_main_verify import answers, quiet


class RewriteImagesTest(unittest.TestCase):
    """Test replacing a Dockerfile's `FROM` images."""

    def test_rewrite(self) -> None:
        """The image word only; flags, stage names and the rest stay as written."""
        text = (
            "# syntax=docker/dockerfile:1\n"
            "FROM --platform=linux/amd64 debian:bookworm AS build\n"
            "RUN echo debian:bookworm\n"
            "from debian:bookworm\n"
            "COPY --from=build /x /x\n"
        )
        new = staging.rewrite_images(text, {"debian:bookworm": "debian:trixie@sha256:ab"}, [])
        self.assertEqual(new, (
            "# syntax=docker/dockerfile:1\n"
            "FROM --platform=linux/amd64 debian:trixie@sha256:ab AS build\n"
            "RUN echo debian:bookworm\n"
            "from debian:trixie@sha256:ab\n"
            "COPY --from=build /x /x\n"
        ))

    def test_left_alone(self) -> None:
        """A mapping to itself, a local parent's tag and an earlier stage aren't images to replace."""
        text = "FROM base AS builder\nFROM builder\nFROM my-parent\n"
        images = {"base": "base", "builder": "evil", "my-parent": "evil"}
        self.assertEqual(staging.rewrite_images(text, images, ["my-parent"]), text)
        self.assertEqual(staging.rewrite_images(text, {}, []), text)

    def test_continued_lines(self) -> None:
        """A `FROM` over several lines is refused when its image is replaced, and kept otherwise."""
        text = "FROM \\\n  debian:bookworm\nRUN true\n"
        with self.assertRaisesRegex(staging.StagingErr, "line 1: a `FROM` continued"):
            staging.rewrite_images(text, {"debian:bookworm": "debian:trixie"}, [])
        self.assertEqual(staging.rewrite_images(text, {"alpine": "alpine:3"}, []), text)


@unittest.skipUnless(HAVE_GENERATOR, NEEDS_GENERATOR)
class PrepareTest(DistTest):
    """Test staging, checking and planning the build, with the generator."""

    def setUp(self) -> None:
        super().setUp()
        (self.files / "env" / "Dockerfile").write_text("FROM debian:bookworm\n", encoding="utf-8")

    def accept(self, edit: Callable[[str], str] | None = None) -> Path:
        """Accept the project's review (edited with `edit`) through the prompt; its `accepted.json`."""
        config = UserConfig(self.project_suc())
        taken = lookup.project_snapshot(config, self.project)

        def editor(path: Path, _: Path) -> None:
            assert edit is not None  # nosec: B101  # narrowing for the type checker
            path.write_text(edit(path.read_text(encoding="ascii")), encoding="ascii")

        prompt = main_verify.Prompt(
            config, taken, None, ask=answers("e", "u") if edit else answers("u"),
            edit=editor if edit else None,
        )
        text = review.build(config, review.Context(
            report=checks.check_project(config), now=prompt.now(), files=taken,
        ))
        with quiet():
            self.assertEqual(prompt.run(text), 0)
        found = lookup.find(config)
        assert found.accepted_settings is not None  # nosec: B101  # narrowing for the type checker
        return found.accepted_settings

    def prepare(
        self, suc: dict, later: datetime.timedelta = datetime.timedelta(),
    ) -> tuple[staging.BuildPlan | None, Path]:
        """Prepare the build, `later` than now."""
        build = self.tmp / "state" / "build"
        build.mkdir(parents=True, mode=0o700)
        now = datetime.datetime.now(datetime.UTC) + later
        return staging.prepare(UserConfig(suc), build, generator.run, now), build

    def test_no_acceptance(self) -> None:
        """Without accepted settings, nothing is staged."""
        plan, build = self.prepare(self.project_suc())
        self.assertIsNone(plan)
        self.assertEqual(os.listdir(build), [])

    def test_builds_from_the_accepted_copy(self) -> None:
        """A change to the live files after the run read them doesn't reach the build."""
        suc = self.project_suc(self.accept())
        (self.files / "env" / "Dockerfile").write_text("FROM evil\n", encoding="utf-8")
        plan, build = self.prepare(suc)
        assert plan is not None  # nosec: B101  # narrowing for the type checker
        self.assertTrue(plan.dev_container.is_relative_to(build))
        self.assertEqual((plan.dev_container / "Dockerfile").read_text(), "FROM debian:bookworm\n")
        self.assertEqual(
            (plan.dev_container / "dev-env.yaml").read_text(encoding="utf-8"), self.dev_env_yaml,
        )
        self.assertEqual(os.stat(plan.dev_container / "Dockerfile").st_mode & 0o777, 0o644)

    def test_changed_before_the_run_read_them(self) -> None:
        """The run read files that aren't the accepted ones: it stops, and says to run again."""
        accepted = self.accept()
        (self.files / "env" / "dev-env.yaml").write_text(
            self.dev_env_yaml.replace("user: ubuntu", "user: root"), encoding="utf-8",
        )
        with self.assertRaisesRegex(staging.StagingErr, "changed while `scoter` was starting") as caught:
            self.prepare(self.project_suc(accepted))
        self.assertFalse(caught.exception.reverify)

    def test_parent_in_the_project(self) -> None:
        """A parent inside the project files directory builds from the copy too."""
        (self.files / "base").mkdir()
        (self.files / "base" / "Dockerfile").write_text("FROM debian:bookworm\n", encoding="utf-8")
        (self.files / "base" / "dev-env.yaml").write_text(self.dev_env_yaml, encoding="utf-8")
        (self.files / "env" / "Dockerfile").write_text("FROM proj-base\n", encoding="utf-8")
        (self.files / "env" / "dev-env.yaml").write_text(
            "parents:\n  - {dir: ../base, tag: proj-base}\n", encoding="utf-8",
        )
        plan, build = self.prepare(self.project_suc(self.accept()))
        assert plan is not None  # nosec: B101  # narrowing for the type checker
        [(tag, parent)] = plan.parents
        self.assertEqual(tag, "proj-base")
        self.assertTrue(parent.is_relative_to(build))
        self.assertEqual((parent / "Dockerfile").read_text(), "FROM debian:bookworm\n")

    def test_expired(self) -> None:
        """An acceptance that has expired since: a new review."""
        suc = self.project_suc(self.accept())
        with self.assertRaisesRegex(staging.StagingErr, "has expired") as caught:
            self.prepare(suc, datetime.timedelta(days=3650))
        self.assertTrue(caught.exception.reverify)

    def test_accepted_file_elsewhere(self) -> None:
        """Accepted settings that aren't an acceptance in the store aren't built from."""
        suc = self.project_suc(self.accept())
        copy = self.tmp / "accepted.json"
        copy.write_text(json.dumps(suc["accepted-settings"]["contents"]), encoding="utf-8")
        suc["accepted-settings"]["file"] = str(copy)
        with self.assertRaisesRegex(staging.StagingErr, "isn't an acceptance in your verified store"):
            self.prepare(suc)

    def test_images(self) -> None:
        """The accepted `images` rewrite the staged Dockerfile; the live one is untouched."""
        accepted = self.accept(lambda text: text.replace(
            '"debian:bookworm": "debian:bookworm"', '"debian:bookworm": "debian:trixie"',
        ))
        plan, _ = self.prepare(self.project_suc(accepted))
        assert plan is not None  # nosec: B101  # narrowing for the type checker
        self.assertEqual((plan.dev_container / "Dockerfile").read_text(), "FROM debian:trixie\n")
        self.assertEqual((self.files / "env" / "Dockerfile").read_text(), "FROM debian:bookworm\n")

    def test_users_container(self) -> None:
        """`use: {name}`: the user's own container, where it is; the project's isn't built."""
        (self.mine / "my-env").mkdir()
        (self.mine / "my-env" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        (self.mine / "my-env" / "dev-env.yaml").write_text(self.dev_env_yaml, encoding="utf-8")
        accepted = self.accept(lambda text: text.replace('use: "project"', 'use: {name: "my-env"}'))
        suc = self.project_suc(accepted)
        self.assertEqual(suc["active-config-locations"]["dev-container-path"], str(self.mine / "my-env"))
        plan, _ = self.prepare(suc)
        assert plan is not None  # nosec: B101  # narrowing for the type checker
        self.assertEqual(plan.dev_container, self.mine / "my-env")
        self.assertEqual(plan.parents, [])


if __name__ == "__main__":
    unittest.main()
