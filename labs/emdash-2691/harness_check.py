#!/usr/bin/env python3
"""Check that the HTTP wrapper returns exactly what the upstream repository returns.

For each version it starts the wrapper, then walks pages twice on the same database file: once over
HTTP and once by calling PluginStorageRepository directly from Node.js. Pages, cursors and flags
must be identical, including the duplicated pages that the pre-fix repository produces. It also
checks that undeclared parameters are rejected and that no request budget is exceeded.

    python harness_check.py --workdir /tmp/cursorproof-emdash-2691
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from run_lab import LAB, harness

WALKS = [
    {"orderBy": "createdAt", "direction": "desc", "limit": "50"},
    {"orderBy": "score", "direction": "asc", "limit": "10"},
    {"limit": "100"},
]


def http_walk(port: int, walk: dict[str, str]) -> list[dict]:
    pages, cursor = [], None
    for _ in range(60):
        query = dict(walk) | ({"cursor": cursor} if cursor else {})
        url = f"http://127.0.0.1:{port}/query?{urllib.parse.urlencode(query)}"
        with urllib.request.urlopen(url, timeout=10) as response:
            page = json.load(response)
        pages.append(page)
        cursor = page["cursor"]
        if not page["hasMore"] or not cursor:
            break
    return pages


def direct_walk(workdir: Path, commit: str, database: Path, walk: dict[str, str]) -> list[dict]:
    command = [
        shutil.which("node") or "node",
        "--disable-warning=ExperimentalWarning",
        "--import",
        str(LAB / "harness/ts-loader.mjs"),
        str(LAB / "harness/direct.mjs"),
        "--snapshot",
        str(workdir / "snapshots" / commit),
        "--db",
        str(database),
        "--fixture",
        str(LAB / "fixture.json"),
        "--limit",
        walk["limit"],
    ]
    if "orderBy" in walk:
        command += ["--orderBy", walk["orderBy"], "--direction", walk["direction"]]
    env = {**os.environ, "EMDASH_LAB_DEPS": str(workdir / "harness-deps")}
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=60, check=True, env=env
    )
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", type=Path, required=True)
    arguments = parser.parse_args()
    workdir = arguments.workdir.resolve()
    provenance = json.loads((LAB / "provenance.json").read_text())
    failures = 0
    for label, info in provenance["versions"].items():
        with tempfile.TemporaryDirectory(prefix="cursorproof-emdash-", dir=workdir) as temporary:
            cleanup: dict[str, object] = {}
            with harness(workdir, info["commit"], Path(temporary), None, cleanup) as (
                port,
                database,
            ):
                for walk in WALKS:
                    over_http = http_walk(port, walk)
                    direct = direct_walk(workdir, info["commit"], database, walk)
                    same = over_http == direct
                    failures += not same
                    print(f"{label:6} {walk}: {len(over_http)} pages, identical={same}")
                for undeclared in ("?sort=score", "?direction=desc", "?limit=abc"):
                    try:
                        urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/query{undeclared}", timeout=5
                        )
                        rejected = False
                    except urllib.error.HTTPError as error:
                        rejected = error.code == 400
                    failures += not rejected
                    print(f"{label:6} {undeclared}: rejected={rejected}")
            print(f"{label:6} cleanup: {cleanup}")
            failures += not (cleanup["process_exited"] and cleanup["port_closed"])
    print("Harness is faithful" if not failures else f"{failures} harness check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
