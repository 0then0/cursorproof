#!/usr/bin/env python3
"""Run CursorProof against a disposable local Distribution Registry with a known tag set.

Requires Docker and a CursorProof installation in the interpreter running this script
(`uv run python labs/registry-tags/run_lab.py`). The script uses an image pinned by digest,
starts a container bound to 127.0.0.1 with in-memory storage, pushes one minimal OCI manifest
under every tag, runs CursorProof, writes configurations, traces and a summary, and always
removes the container. Nothing is pushed to any other registry.

This is an integration check of Link header continuation against the real tags/list API. It
does not reproduce or claim any registry defect.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml
from fixture import PARTIAL_REPOSITORY, REPOSITORY, TAGS, WITHHELD, expected_tags, partial_tags

LAB = Path(__file__).resolve().parent
IMAGE = "registry:3.0.0@sha256:6c5666b861f3505b116bb9aa9b25175e71210414bd010d92035ff64018f9457e"
LIMITS = [1, 5, 8, 24, 100]
CONTAINER = "cursorproof-registry-lab"
CONFIG_BLOB = b"{}"
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], text=True, capture_output=True, check=check)


def call(
    url: str, method: str = "GET", data: bytes | None = None, content_type: str | None = None
) -> tuple[int, dict[str, list[str]], bytes]:
    headers = {"Content-Type": content_type} if content_type else {}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        fields: dict[str, list[str]] = {}
        for name, value in response.headers.items():
            fields.setdefault(name.lower(), []).append(value)
        return response.status, fields, response.read()


def wait_ready(base: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            call(base + "/v2/")
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    raise SystemExit("Registry did not become ready")


def push(base: str, repository: str, tags: list[str]) -> None:
    """One config blob and one layerless manifest, stored once and referenced by every tag."""
    digest = "sha256:" + hashlib.sha256(CONFIG_BLOB).hexdigest()
    _, fields, _ = call(f"{base}/v2/{repository}/blobs/uploads/", "POST", b"")
    location = fields["location"][0]
    if location.startswith("/"):
        location = base + location
    separator = "&" if "?" in location else "?"
    call(f"{location}{separator}digest={digest}", "PUT", CONFIG_BLOB, "application/octet-stream")
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": MANIFEST_TYPE,
            "config": {
                "mediaType": "application/vnd.oci.empty.v1+json",
                "digest": digest,
                "size": len(CONFIG_BLOB),
            },
            "layers": [],
        }
    ).encode()
    for tag in tags:
        call(f"{base}/v2/{repository}/manifests/{tag}", "PUT", manifest, MANIFEST_TYPE)


def config(base: str, repository: str) -> dict[str, object]:
    return {
        "url": f"{base}/v2/{repository}/tags/list",
        "pagination": {
            "mode": "url",
            "continuation": "link-header",
            "cursor_param": "last",
            "limit_param": "n",
        },
        "response": {"items": "$.tags", "id": "$"},
        "ordering": [{"field": "$", "type": "string"}],
        "limits": LIMITS,
        "repeats": 1,
        "max_pages": 1000,
        "max_items": 10000,
        "oracle": {"command": ["python3", "../oracle.py"], "format": "json", "ordered": True},
    }


def cursorproof(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cursorproof", *args], text=True, capture_output=True, check=False
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=15000)
    parser.add_argument("--output", type=Path, default=LAB / "evidence")
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    configs = LAB / "configs"
    traces = args.output / "traces"
    for directory in (configs, traces):
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)

    docker("rm", "-f", "-v", CONTAINER, check=False)
    run_command = [
        "run", "-d", "--rm", "--name", CONTAINER, "--tmpfs", "/var/lib/registry",
        "-p", f"127.0.0.1:{args.port}:5000", IMAGE,
    ]  # fmt: skip
    commands = [
        "docker " + " ".join(run_command),
        f"POST+PUT {base}/v2/{REPOSITORY}/blobs/uploads/ (one {len(CONFIG_BLOB)}-byte config)",
        f"PUT {base}/v2/{REPOSITORY}/manifests/<tag> ({len(TAGS)} tags, one layerless manifest)",
        f"the same for {PARTIAL_REPOSITORY} without {WITHHELD}",
        f"docker rm -f -v {CONTAINER}",
    ]
    summary: dict[str, object] = {"image": IMAGE, "commands": commands, "runs": {}}
    outcomes: list[bool] = []
    try:
        docker(*run_command)
        wait_ready(base)
        summary["registry_version"] = docker(
            "exec", CONTAINER, "registry", "--version"
        ).stdout.strip()
        summary["container_mounts"] = docker(
            "inspect", CONTAINER, "--format", "{{range .Mounts}}{{.Type}} {{end}}"
        ).stdout.split()
        push(base, REPOSITORY, TAGS)
        push(base, PARTIAL_REPOSITORY, partial_tags())

        # Direct observation of the API contract, independent of CursorProof.
        first = f"/v2/{REPOSITORY}/tags/list?n=5"
        _, fields, body = call(base + first)
        last_tag = expected_tags()[-1]
        final = f"/v2/{REPOSITORY}/tags/list?n=5&last={expected_tags()[-2]}"
        _, final_fields, final_body = call(base + final)
        summary["api_observations"] = [
            {"request": "GET " + first, "link": fields.get("link", []), "body": json.loads(body)},
            {
                "request": "GET " + final,
                "link": final_fields.get("link", []),
                "body": json.loads(final_body),
            },
        ]
        outcomes.append(len(fields.get("link", [])) == 1 and 'rel="next"' in fields["link"][0])
        outcomes.append(
            not final_fields.get("link") and json.loads(final_body)["tags"] == [last_tag]
        )

        scenarios = [
            ("tags", REPOSITORY, expected_tags(), 0),
            # Control: the same oracle against a repository that lacks three tags.
            ("tags-partial", PARTIAL_REPOSITORY, sorted(partial_tags()), 1),
        ]
        runs: dict[str, object] = {}
        for name, repository, expected, expected_exit in scenarios:
            path = configs / f"{name}.yml"
            path.write_text(yaml.safe_dump(config(base, repository), sort_keys=False))
            trace = traces / f"{name}.json"
            result = cursorproof("run", str(path), "--format", "json", "--repro", str(trace))
            report = json.loads(result.stdout)
            offline = cursorproof("replay", str(trace), "--format", "json")
            live = cursorproof(
                "replay", str(trace), "--config", str(path), "--traversal", "2",
                "--execute-hooks", "--format", "json",
            )  # fmt: skip
            traversals = report["trace"]["traversals"]
            streams = [
                [item["id"] for page in t["pages"] for item in page["items"]] for t in traversals
            ]
            exact = all(stream == expected for stream in streams)
            offline_report = json.loads(offline.stdout)
            same_offline = all(
                offline_report[key] == report[key]
                for key in ("outcome", "summary", "findings", "errors")
            )
            runs[name] = {
                "exit_code": result.returncode,
                "findings": sorted({f["code"] for f in report["findings"]}),
                "errors": [error["message"] for error in report["errors"]],
                "traversals": report["summary"]["traversals"],
                "pages_per_limit": {str(t["limit"]): len(t["pages"]) for t in traversals},
                "stops": sorted({t["stop"] for t in traversals}),
                "unique_items": report["summary"]["unique_items"],
                "repository_items": len(expected),
                "oracle_items": len(expected_tags()),
                "every_stream_equals_repository_tags_in_order": exact,
                "offline_replay_exit_code": offline.returncode,
                "offline_replay_equals_run": same_offline,
                "live_replay_exit_code": live.returncode,
            }
            ok = result.returncode == expected_exit and offline.returncode == expected_exit
            ok = ok and live.returncode == expected_exit and exact and same_offline
            outcomes.append(ok)
        summary["runs"] = runs
    finally:
        docker("rm", "-f", "-v", CONTAINER, check=False)
    remaining = docker("ps", "-aq", "--filter", f"name=^{CONTAINER}$", check=False).stdout.strip()
    summary["container_removed"] = remaining == ""
    summary["passed"] = (
        all(outcomes)
        and len(outcomes) == 4
        and remaining == ""
        and "volume" not in summary.get("container_mounts", ["volume"])  # type: ignore[operator]
    )
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary["runs"], indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
