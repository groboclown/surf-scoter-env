#SPDX:MIT
"""Starting the network gateway: its certificate authority before, and its
post-start run after.

Every run gets a new CA, whatever the gateway: `scoter run` makes it in the
run's resources directory, the docker-compose file gives its certificate to
the dev container, and the run removes it when it ends.  Only a gateway that
looks inside TLS (`dev-env: values: network-gateway-reads-tls`) gets the
CA's directory, key and all; any other (Squid) gets an empty one.

The gateway runs as its own user, which can't read a file only you can (with
Docker, it isn't you; with rootless Podman, it's one of your sub-ids), so a
key it's given is readable by others, inside a directory that's readable by
others.  Something above them must be yours alone, so only you, and the
gateway through its mount, can reach the key.

Some gateways need something once they're running, such as the seekrit
gateway's secrets, which are never in its configuration, environment, or
files: the setup's `dev-env: runs: network-gateway-post-start` gives them.
"""
import os
from pathlib import Path

from . import fileutil
from .err import ConfigErr
from .user_config import Capture, OnError, UserConfig

# In the run's resources directory.
CA_DIR = "ca"
CA_CERT_FILE = "gateway-ca.pem"
CA_KEY_FILE = "gateway-ca-key.pem"
# What a gateway that doesn't read TLS gets in place of the CA's directory.
NO_CA_DIR = "gateway-no-ca"
# The certificate is public: the dev container trusts it.
MODE_CA_CERT = 0o644
# The key signs certificates the dev container trusts, for any host: only
# yours, unless the gateway must read it (see above).
MODE_CA_DIR = 0o700
MODE_CA_KEY = 0o600
MODE_SHARED_CA_DIR = 0o755
MODE_SHARED_CA_KEY = 0o644

READS_TLS = "network-gateway-reads-tls"

POST_START = "network-gateway-post-start"


def reads_tls(config: UserConfig) -> bool:
    """Whether the setup's gateway looks inside TLS, and so needs the CA's key."""
    try:
        value = config.require_value("dev-env", READS_TLS)
    except ConfigErr:
        return False
    answer = value.strip().lower()
    if answer in ("yes", "true"):
        return True
    if answer in ("no", "false"):
        return False
    raise ConfigErr(f"The setup's `dev-env: values: {READS_TLS}` must be 'yes' or 'no', not {value!r}.")


def private_ancestor(path: Path) -> Path | None:
    """The nearest directory above `path` that's yours, and only you can use; None without one."""
    for parent in Path(os.path.abspath(path)).parents:
        try:
            info = os.stat(parent)
        except OSError:
            continue
        if info.st_uid == os.getuid() and not info.st_mode & 0o077:
            return parent
    return None


def make_ca(config: UserConfig, resources_dir: Path, *, shared: bool = False) -> tuple[Path, Path]:
    """A new CA for this run, in `resources_dir`: its certificate and key files.

    Any CA left by an earlier run is removed first, so a key is never reused.
    `shared`: the gateway reads the key, so it's readable by others, which
    needs a directory above that only you can use.
    """
    directory = resources_dir / CA_DIR
    if shared and not fileutil.is_windows() and private_ancestor(directory) is None:
        raise ConfigErr(
            f"The network gateway must read its CA's key, so the key in {directory} can't be "
            "private to you; nothing above it is, either.  Keep `project-state: paths: "
            "resources-dir` inside a directory only you can use (`chmod 700`), as "
            "`project-state-dir` is by default."
        )
    remove_ca(resources_dir)
    if directory.exists():
        raise ConfigErr(f"Couldn't remove the last run's gateway CA in {directory}.")
    dir_mode = MODE_SHARED_CA_DIR if shared else MODE_CA_DIR
    directory.mkdir(mode=dir_mode)
    os.chmod(directory, dir_mode)
    cert, key = directory / CA_CERT_FILE, directory / CA_KEY_FILE
    res = config.run_command(
        "dev-env", "network-gateway-mk-ca",
        template={"ca-cert": str(cert), "ca-key": str(key)},
        capture=Capture.BOTH,
        on_error=OnError.RETURN,
    )
    if res.returncode != 0 or not cert.is_file() or not key.is_file():
        detail = (res.stderr or b"").decode("utf-8", "replace").strip()
        raise ConfigErr(f"Couldn't make the network gateway's CA (`dev-env: runs: network-gateway-mk-ca`): {detail}")
    os.chmod(key, MODE_SHARED_CA_KEY if shared else MODE_CA_KEY)
    os.chmod(cert, MODE_CA_CERT)
    return cert, key


def gateway_ca_dir(resources_dir: Path, shared: bool) -> Path:
    """What the gateway's `/user-space/ca` mounts: the CA's directory, if it
    reads TLS, or else an empty directory."""
    if shared:
        return resources_dir / CA_DIR
    empty = resources_dir / NO_CA_DIR
    fileutil.rm(empty)
    empty.mkdir(mode=MODE_SHARED_CA_DIR)
    return empty


def remove_ca(resources_dir: Path) -> None:
    """Remove the run's CA, if it's there."""
    fileutil.rm(resources_dir / CA_DIR)


def post_start(config: UserConfig, universal: Path, container: str) -> bool:
    """Run the setup's `network-gateway-post-start`, if it has one; whether it worked."""
    if not config.has_run("dev-env", POST_START):
        return True
    print("Starting the network gateway's post-start...")
    res = config.run_command(
        "dev-env", POST_START,
        template={"universal": str(universal), "gateway-container": container},
        capture=Capture.NONE,
        on_error=OnError.RETURN,
    )
    return res.returncode == 0
