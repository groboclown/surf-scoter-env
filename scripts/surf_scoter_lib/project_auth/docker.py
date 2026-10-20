#SPDX:MIT
"""Generates a fingerprint from a Dockerfile.

The Dockerfile itself isn't enough to fingerprint.  It can have related files
loaded into the container from the local filesystem, and it can have parents
that then need their own fingerprinting (see dev-env.yaml file format).
"""

from .algo import gen_common
from .auth_project_file import ResourceFingerprint


def fingerprint_dockerfile(resource: str, contents: str) -> ResourceFingerprint:
    """Construct the fingerprints for the Dockerfile contents."""
    relevant = ""
    for line in contents.splitlines():
        parsed = line.strip()
        if not parsed or parsed.startswith("#"):
            continue
        relevant += parsed
        if not parsed.endswith("\\"):
            relevant += "\n"

    return ResourceFingerprint(
        resource=resource,
        fingerprints=gen_common(relevant.encode("utf-8")),
    )


# def fingerprint_docker_ref(resource: str, )
