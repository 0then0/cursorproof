#!/usr/bin/env python3
"""Independent expected-order oracle for the EmDash #2691 lab.

Reads the disposable SQLite database directly (read-only), checks that its documents are exactly
the documents in fixture.json, and prints the expected ids, one per line. It never calls the
repository under test and never follows a cursor.

Rules (the contract that the fixed repository documents):
  * Sort values are compared as JSON values: numbers numerically, strings by Unicode code point.
    ISO timestamps with a fixed UTC format therefore compare chronologically.
  * A null or missing sort value ranks after every value ascending and before every value
    descending.
  * Records with equal sort values are ordered by id, by code point, in the same direction.
  * Without --sort the order is the stored (created_at, id) ascending, both read from the database.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def load_fixture(path: Path) -> dict:
    return json.loads(path.read_text())


def read_database(database: Path, plugin_id: str, collection: str) -> dict[str, tuple[str, dict]]:
    uri = f"file:{database.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        rows = connection.execute(
            "SELECT id, created_at, data FROM _plugin_storage "
            "WHERE plugin_id = ? AND collection = ?",
            (plugin_id, collection),
        ).fetchall()
    finally:
        connection.close()
    return {row_id: (created_at, json.loads(data)) for row_id, created_at, data in rows}


def expected_order(
    rows: dict[str, tuple[str, dict]], sort: str | None, direction: str
) -> list[str]:
    if sort is None:
        return [
            row_id
            for _, row_id in sorted((created, row_id) for row_id, (created, _) in rows.items())
        ]

    def key(row_id: str) -> tuple[int, object, str]:
        value = rows[row_id][1].get(sort)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float, str))
        ):
            raise SystemExit(f"Unsupported sort value type for {row_id}")
        return (1, 0, row_id) if value is None else (0, value, row_id)

    try:
        return sorted(rows, key=key, reverse=direction == "desc")
    except TypeError:
        raise SystemExit("Sort field mixes incompatible value types") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--sort", help="Data field to sort by; omit for the default order.")
    parser.add_argument("--direction", choices=("asc", "desc"), default="asc")
    arguments = parser.parse_args()

    fixture = load_fixture(arguments.fixture)
    rows = read_database(arguments.db, fixture["plugin_id"], fixture["collection"])
    declared = {document["id"]: document["data"] for document in fixture["documents"]}
    actual = {row_id: data for row_id, (_, data) in rows.items()}
    if actual != declared:
        print("Database documents differ from the fixture", file=sys.stderr)
        return 3
    if arguments.sort is None and arguments.direction != "asc":
        print("The default order is ascending only", file=sys.stderr)
        return 2
    sys.stdout.write(
        "".join(
            f"{row_id}\n" for row_id in expected_order(rows, arguments.sort, arguments.direction)
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
