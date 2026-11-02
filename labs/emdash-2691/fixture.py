"""Deterministic synthetic fixture for the EmDash #2691 lab.

The lab never imports this module into the harness: the harness loads the JSON file it writes,
and the oracle verifies the database against the same file.

    python3 fixture.py            # rewrite fixture.json
    python3 fixture.py --check    # exit 1 when fixture.json differs from the generator
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN_ID = "cursorproof-lab"
COLLECTION = "submissions"
INDEXES = ["createdAt", "score", "note"]
COUNT = 150
FIXTURE_PATH = Path(__file__).with_name("fixture.json")


def documents() -> list[dict[str, object]]:
    """Return documents in insertion order; ids ascend with insertion order.

    createdAt: unique, ascending with insertion order (like form submissions).
    score:     50 distinct values, three records each, unrelated to insertion order.
    note:      explicit JSON null for every fifth record, otherwise 17 distinct values.
    """
    start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    result: list[dict[str, object]] = []
    for index in range(1, COUNT + 1):
        created = start + timedelta(minutes=7 * index)
        result.append(
            {
                "id": f"sub-{index:03d}",
                "data": {
                    "createdAt": created.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "score": (index * 37) % 50,
                    "note": None if index % 5 == 0 else (index * 13) % 17,
                },
            }
        )
    return result


def build() -> dict[str, object]:
    return {
        "plugin_id": PLUGIN_ID,
        "collection": COLLECTION,
        "indexes": INDEXES,
        "documents": documents(),
    }


def render() -> str:
    return json.dumps(build(), indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="Verify fixture.json is current.")
    arguments = parser.parse_args()
    text = render()
    if arguments.check:
        if FIXTURE_PATH.read_text() != text:
            print("fixture.json differs from fixture.py", file=sys.stderr)
            return 1
        return 0
    FIXTURE_PATH.write_text(text)
    print(f"{FIXTURE_PATH.name}: {COUNT} documents, sha256 {sha256_file(FIXTURE_PATH)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
