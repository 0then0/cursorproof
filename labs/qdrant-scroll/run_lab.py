#!/usr/bin/env python3
"""Run CursorProof against a disposable local Qdrant with known integer and UUID point IDs.

Requires Docker and a CursorProof installation in the interpreter running this script
(`uv run python labs/qdrant-scroll/run_lab.py`). The script pulls a pinned image, starts a
container bound to 127.0.0.1, creates two new collections, runs CursorProof, writes sanitized
configurations, traces and a summary, and always removes the container.

This is an integration check of the POST/JSON-body mode against the real Scroll API. It does not
reproduce or claim any Qdrant defect.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml
from fixture import (
    FILTER,
    INTS,
    UUIDS,
    VECTOR_SIZE,
    expected_ints,
    expected_uuids,
    int_points,
    uuid_points,
)

LAB = Path(__file__).resolve().parent
IMAGE = "qdrant/qdrant:v1.20.0"
LIMITS = [1, 7, 10, 25, 100]
CONTAINER = "cursorproof-qdrant-lab"


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], text=True, capture_output=True, check=check)


def call(base: str, method: str, path: str, body: object = None) -> dict[str, object]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def wait_ready(base: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base + "/readyz", timeout=2):
                return
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    raise SystemExit("Qdrant did not become ready")


def create_collection(base: str, name: str, points: list[tuple[object, dict[str, object]]]) -> None:
    call(base, "PUT", f"/collections/{name}", {"vectors": {"size": VECTOR_SIZE, "distance": "Dot"}})
    call(
        base,
        "PUT",
        f"/collections/{name}/points?wait=true",
        {
            "points": [
                {"id": point_id, "vector": [0.1, 0.2, 0.3, float(index)], "payload": payload}
                for index, (point_id, payload) in enumerate(points)
            ]
        },
    )
    call(
        base,
        "PUT",
        f"/collections/{name}/index?wait=true",
        {"field_name": "group", "field_schema": "keyword"},
    )


def config(base: str, collection: str, kind: str, *, filtered: bool = True) -> dict[str, object]:
    body: dict[str, object] = {"with_payload": ["group", "rank"], "with_vector": False}
    if filtered:
        body = {"filter": FILTER, **body}
    return {
        "url": f"{base}/collections/{collection}/points/scroll",
        "method": "POST",
        "body": body,
        "pagination": {
            "mode": "token",
            "cursor_param": "offset",
            "limit_param": "limit",
            "terminal_values": [None],
        },
        "response": {
            "items": "$.result.points",
            "next_cursor": "$.result.next_page_offset",
            "id": "$.id",
        },
        "ordering": [{"field": "id", "type": "number" if kind == "ints" else "string"}],
        "limits": LIMITS,
        "repeats": 1,
        "max_pages": 1000,
        "max_items": 10000,
        "oracle": {"command": ["python3", "../oracle.py", kind], "format": "json", "ordered": True},
    }


def cursorproof(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cursorproof", *args], text=True, capture_output=True, check=False
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=16333)
    parser.add_argument("--output", type=Path, default=LAB / "evidence")
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    configs = LAB / "configs"
    traces = args.output / "traces"
    for directory in (configs, traces):
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)

    docker("rm", "-f", CONTAINER, check=False)
    commands = [
        f"docker run -d --rm --name {CONTAINER} -p 127.0.0.1:{args.port}:6333 {IMAGE}",
        f"PUT {base}/collections/{INTS} (vectors size {VECTOR_SIZE}, Dot)",
        f"PUT {base}/collections/{INTS}/points?wait=true ({len(int_points())} integer-ID points)",
        f"PUT {base}/collections/{INTS}/index?wait=true (keyword index on payload.group)",
        f"PUT {base}/collections/{UUIDS} and its points/index ({len(uuid_points())} UUID points)",
    ]
    summary: dict[str, object] = {"image": IMAGE, "commands": commands, "runs": {}}
    outcomes: list[bool] = []
    try:
        docker("run", "-d", "--rm", "--name", CONTAINER, "-p", f"127.0.0.1:{args.port}:6333", IMAGE)
        wait_ready(base)
        info = call(base, "GET", "/")
        summary["qdrant"] = info
        summary["image_digest"] = docker(
            "image", "inspect", IMAGE, "--format", "{{index .RepoDigests 0}}"
        ).stdout.strip()
        create_collection(base, INTS, int_points())  # type: ignore[arg-type]
        create_collection(base, UUIDS, uuid_points())  # type: ignore[arg-type]
        scenarios = [
            ("ints", INTS, "ints", True, 0),
            ("uuids", UUIDS, "uuids", True, 0),
            # Control: without the filter the unfiltered points appear and the oracle rejects them.
            ("ints-without-filter", INTS, "ints", False, 1),
        ]
        runs: dict[str, object] = {}
        for name, collection, kind, filtered, expected_exit in scenarios:
            path = configs / f"{name}.yml"
            path.write_text(
                yaml.safe_dump(config(base, collection, kind, filtered=filtered), sort_keys=False)
            )
            trace = traces / f"{name}.json"
            result = cursorproof("run", str(path), "--format", "json", "--repro", str(trace))
            report = json.loads(result.stdout)
            offline = cursorproof("replay", str(trace), "--format", "json")
            live = cursorproof(
                "replay", str(trace), "--config", str(path), "--traversal", "2",
                "--execute-hooks", "--format", "json",
            )  # fmt: skip
            expected = (expected_ints if kind == "ints" else expected_uuids)()
            streams = [
                [item["id"] for page in t["pages"] for item in page["items"]]
                for t in report["trace"]["traversals"]
            ]
            exact = all(stream == expected for stream in streams)
            runs[name] = {
                "exit_code": result.returncode,
                "findings": sorted({f["code"] for f in report["findings"]}),
                "traversals": report["summary"]["traversals"],
                "pages": report["summary"]["pages"],
                "unique_items": report["summary"]["unique_items"],
                "expected_items": len(expected),
                "every_stream_equals_expected_ids": exact,
                "offline_replay_exit_code": offline.returncode,
                "live_replay_exit_code": live.returncode,
            }
            ok = result.returncode == expected_exit and offline.returncode == expected_exit
            ok = ok and live.returncode == expected_exit
            ok = ok and (exact if expected_exit == 0 else True)
            outcomes.append(ok)
        summary["runs"] = runs
    finally:
        docker("rm", "-f", CONTAINER, check=False)
    summary["passed"] = all(outcomes) and len(outcomes) == 3
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary["runs"], indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
