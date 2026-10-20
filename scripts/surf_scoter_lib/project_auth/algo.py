#SPDX:MIT
"""Fingerprint computation algorithms."""

import hashlib
from .auth_project_file import Fingerprint


def gen_common(data: bytes) -> list[Fingerprint]:
    """Construct the common set of fingerprints."""
    return [
        Fingerprint(algorithm="size", fingerprint=str(len(data))),
        Fingerprint(algorithm="sha256", fingerprint=hashlib.sha256(data).hexdigest()),
        Fingerprint(algorithm="sha3-512", fingerprint=hashlib.sha3_512(data).hexdigest()),
        Fingerprint(algorithm="shake-256", fingerprint=hashlib.shake_256(data).hexdigest(20)),
    ]
