#SPDX:MIT
"""Find the user's acceptance for the project in a universal configuration file.

Usage: verified_find.py UNIVERSAL.json

The setup's `verified: runs: find-acceptance` runs this for `scoter`.  It
prints JSON: the match (`exact`, `meaning`, `blanket` or null), the
acceptance's id, its `accepted.json` (for `gen-scoter-uc
--accepted-settings`), and any notice for the user; see
`surf_scoter_lib.verified.lookup`.
"""
import json
import sys
from collections.abc import Sequence

from surf_scoter_lib import UserConfig
from surf_scoter_lib.err import SurfScoterErr
from surf_scoter_lib.verified import lookup

MAX_UNIVERSAL_SIZE = 4 * 1024 * 1024


def main(argv: Sequence[str]) -> int:
    """CLI entrypoint."""
    if len(argv) != 2:
        print(f"Usage: {argv[0] if argv else 'verified_find.py'} UNIVERSAL.json", file=sys.stderr)
        return 2
    try:
        with open(argv[1], "rb") as inp:
            data = inp.read(MAX_UNIVERSAL_SIZE + 1)
        if len(data) > MAX_UNIVERSAL_SIZE:
            raise SurfScoterErr(f"{argv[1]}: larger than {MAX_UNIVERSAL_SIZE} bytes")
        suc = json.loads(data)
        if not isinstance(suc, dict):
            raise SurfScoterErr(f"{argv[1]}: not a universal configuration file")
        found = lookup.find(UserConfig(suc))
    except (OSError, ValueError, SurfScoterErr) as err:
        print(f"{argv[0]}: {err}", file=sys.stderr)
        return 1
    print(json.dumps(found.to_json()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
