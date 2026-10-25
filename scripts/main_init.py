#SPDX:MIT
"""Surf Scoter Sub-command: init

Set up a project to work with Surf Scoter.

Currently, this creates just the bare bones.  Eventually, this may grow
to allow creating examples for different project types.
"""
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from surf_scoter_lib import (
    BAD_EXECUTION_ARGS_CODE,
    UserConfig,
    gen_parser,
    log,
)
from surf_scoter_lib.err import SurfScoterErr


def run_init(config: UserConfig) -> int:
    """Construct the basic environment layout."""
    # May want to put these into the distribution's `defaults` directory,
    # so the distribution can modify them.
    cwd = Path(os.curdir)
    scoter_dir_name = config.require_value("scoter", "project-files-dir")
    project_file_name = config.require_value("scoter", "project-file-name")
    scoter_dir = cwd / scoter_dir_name
    if scoter_dir.exists():
        log.error(f"You already have a {scoter_dir_name} directory here.")
        return 1

    scoter_dir.mkdir(parents=True)
    (scoter_dir / README_FILE).write_text(INITIAL_README, encoding="utf-8")
    (scoter_dir / project_file_name).write_text(
        INITIAL_PROJECT.replace("@FOUND_VCS@", find_vcs(cwd)),
        encoding="utf-8",
    )

    image_prefix = config.require_value("dev-env", "dev-env-image-prefix")
    (scoter_dir / DEFAULT_DOCKERFILE_FILE).parent.mkdir(parents=True)
    (scoter_dir / DEFAULT_DOCKERFILE_FILE).write_text(
        DEFAULT_DOCKERFILE.replace("@DEFAULT_PREFIX@", image_prefix),
        encoding="utf-8",
    )
    (scoter_dir / CONTAINER_YAML_FILE).write_text(
        CONTAINER_YAML.replace("@DEFAULT_PREFIX@", image_prefix),
        encoding="utf-8",
    )

    return 0


README_FILE = "README.md"
INITIAL_README = """# Surf Scoter Development Environment

You'll need [Surf Scoter](https://github.com/groboclown/surf-scoter-env)
installed locally to use this.

- [project.yaml](project.yaml) - additional permissions to run functionality from
  the container.
- [dev-env-container](dev-env-container/Dockerfile) - container that creates a
  good starting point for developing this project.

"""

INITIAL_PROJECT = """
vcs: @FOUND_VCS@

# Location of the container files that run a dev environment.
container:
  dev-container-path: dev-env-container
  workspace-subdir: proj

# Where the dev container may connect, through the network gateway.
egress:
  # Reach out to PyPI for downloading Python packages.
  - {host: pypi.org, protocol: tls}
  - {host: files.pythonhosted.org, protocol: tls}
"""

DEFAULT_DOCKERFILE_FILE = "dev-env-container/Dockerfile"
DEFAULT_DOCKERFILE = """FROM @DEFAULT_PREFIX@dev-env:distro

# Install additional system packages.
USER root
RUN set -x \
 && apt-get update \
 && apt-get upgrade -y \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        tmux \
 && rm -rf /var/lib/apt/lists/*

# End with user-mode installs and setup.
USER ubuntu
RUN set -x \
# The ownership of the mounted /workspace directory will trigger git messages.
 && git config --global --add safe.directory /workspace/proj
"""

CONTAINER_YAML_FILE = "dev-env-container/dev-env.yaml"
CONTAINER_YAML = """
# This inherits all settings from the parent.

parents:
  - name: dev-env
    # This tag comes from the Makefile
    tag: @DEFAULT_PREFIX@dev-env:distro
"""

# Generally good signal of the current VCS.
VCS_DIR_MAP = {
    "git": ".git",
    "hg": ".hg",
    "cvs": "CVS",
    "svn": ".svn",
}

# Signs that maybe you're in a VCS.
VCS_FILE_MAP: dict[str, Sequence[str]] = {
    "git": (".gitignore",),
    "hg": (".hgignore",),
    "p4": (".p4config", ".p4ignore"), # if you're lucky
}
DEFAULT_VCS = "file"


def find_vcs(cwd: Path) -> str:
    """Best-effort to find the VCS."""
    for name, subdir in VCS_DIR_MAP.items():
        if (cwd / subdir).is_dir():
            return name
    for name, items in VCS_FILE_MAP.items():
        for file in items:
            if (cwd / file).is_file():
                return name
    # Nothing found.
    return DEFAULT_VCS


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    config, argv, parser = gen_parser(argv, (
        "Construct a simple Surf Scoter directory for the project in the current "
        "directory."
    ))
    if not config:
        return BAD_EXECUTION_ARGS_CODE

    # Even though the arguments aren't used, this requires to parse them to get the
    # logging configured right.
    parser.parse_args(argv)
    return run_init(config)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
