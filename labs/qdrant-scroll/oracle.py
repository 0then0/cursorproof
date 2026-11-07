"""Print the independently known, filtered point IDs as a JSON array."""

from __future__ import annotations

import json
import sys

from fixture import expected_ints, expected_uuids

if __name__ == "__main__":
    kinds = {"ints": expected_ints, "uuids": expected_uuids}
    if len(sys.argv) != 2 or sys.argv[1] not in kinds:
        raise SystemExit("usage: oracle.py ints|uuids")
    print(json.dumps(kinds[sys.argv[1]]()))
