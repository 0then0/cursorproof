#!/usr/bin/env python3
"""Run the EmDash #2691 lab: real PluginStorageRepository, before and after the fix.

Run with the interpreter that has CursorProof installed (an installed wheel or `uv run`). The
network is not used here; run prepare.py first. Each repetition gets a fresh SQLite database and a
fresh harness process on an ephemeral loopback port. Everything is stopped and removed on success,
failure and interruption.

    python run_lab.py --workdir /tmp/cursorproof-emdash-2691 --output /tmp/emdash-2691-evidence
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import queue
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import yaml
from lab_metrics import is_clean, report_signature

LAB = Path(__file__).resolve().parent
REPOSITORY_ROOT = LAB.parents[1]
SCENARIOS = [
    "default-order",
    "created-asc",
    "created-desc",
    "score-asc",
    "score-desc",
    "note-asc",
    "note-desc",
]
# Scenarios whose contract also holds before the fix: the sort field agrees with insertion order
# (or there is no orderBy), so the old created_at seek happens to be consistent.
CONTROLS_BEFORE = {"default-order", "created-asc"}
DEFAULT_TRACES = [
    "before:default-order",
    "before:created-desc",
    "after:created-desc",
    "before:score-desc",
    "after:score-desc",
]
READY_DEADLINE = 90
CLI_DEADLINE = 180


def interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def tool_version(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    return result.stdout.strip() or result.stderr.strip()


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


@contextmanager
def harness(
    workdir: Path, commit: str, scratch: Path, manifest: Path | None, cleanup: dict[str, object]
):
    """Start the real repository behind the wrapper; always stop it and report cleanup."""
    database = scratch / "emdash.sqlite3"
    stderr_path = scratch / "harness.stderr"
    command = [
        shutil.which("node") or "node",
        "--disable-warning=ExperimentalWarning",
        "--import",
        str(LAB / "harness/ts-loader.mjs"),
        str(LAB / "harness/server.mjs"),
        "--snapshot",
        str(workdir / "snapshots" / commit),
        "--db",
        str(database),
        "--fixture",
        str(LAB / "fixture.json"),
        "--port",
        "0",
        "--max-requests",
        "1500",
        "--max-lifetime-seconds",
        "600",
        "--exit-on-stdin-close",
    ]
    if manifest is not None:
        command += ["--manifest-out", str(manifest)]
    env = {**os.environ, "EMDASH_LAB_DEPS": str(workdir / "harness-deps")}
    with stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            env=env,
            start_new_session=True,
        )
        port = None
        try:
            lines: queue.Queue[str] = queue.Queue()
            assert process.stdout is not None
            threading.Thread(
                target=lambda: lines.put(process.stdout.readline().decode()), daemon=True
            ).start()
            try:
                started = json.loads(lines.get(timeout=READY_DEADLINE))
            except (queue.Empty, json.JSONDecodeError):
                raise RuntimeError(
                    f"Harness did not start: {stderr_path.read_text()[-2000:]}"
                ) from None
            port = started["port"]
            deadline = time.monotonic() + READY_DEADLINE
            while True:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
                        if json.load(r).get("ready"):
                            break
                except OSError:
                    pass
                if time.monotonic() > deadline or process.poll() is not None:
                    raise RuntimeError("Harness readiness check failed")
                time.sleep(0.2)
            yield port, database
        finally:
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            if process.stdout is not None:
                process.stdout.close()
            cleanup["process_exited"] = process.poll() is not None
            cleanup["port_closed"] = port is None or not port_open(port)


def build_config(name: str, port: int, database: Path, destination: Path) -> Path:
    config = yaml.safe_load((LAB / "configs" / f"{name}.yml").read_text())
    config["url"] = f"http://127.0.0.1:{port}/query"
    command = config["oracle"]["command"]
    command[0] = sys.executable
    config["oracle"]["command"] = [
        str(database)
        if part == "${EMDASH_LAB_DB}"
        else str(LAB / part[3:])
        if part.startswith("../")
        else part
        for part in command
    ]
    destination.write_text(json.dumps(config, indent=2) + "\n")
    return destination


def cli(arguments: list[str]) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, "-m", "cursorproof", *arguments, "--format", "json"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=CLI_DEADLINE,
        check=False,
    )
    if result.returncode not in {0, 1, 2}:
        raise RuntimeError(f"CursorProof process failed: {result.stderr[-2000:]}")
    return result.returncode, json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--workdir", type=Path, required=True, help="Directory prepared by prepare.py."
    )
    parser.add_argument("--output", type=Path, required=True, help="Evidence directory to write.")
    parser.add_argument("--repetitions", type=int, default=3, help="Fresh databases per version.")
    parser.add_argument(
        "--traces",
        nargs="*",
        default=DEFAULT_TRACES,
        metavar="VERSION:SCENARIO",
        help="Traces from the first repetition to keep as evidence.",
    )
    parser.add_argument("--run-deadline", type=int, default=1800, help="Total seconds allowed.")
    arguments = parser.parse_args()
    if not 1 <= arguments.repetitions <= 10:
        parser.error("--repetitions must be between 1 and 10")
    workdir = arguments.workdir.resolve()
    output = arguments.output.resolve()
    provenance = json.loads((LAB / "provenance.json").read_text())
    if not (workdir / "harness-deps/node_modules/kysely").is_dir():
        raise SystemExit("Run prepare.py first")
    for sub in ("traces",):
        (output / sub).mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, interrupt)
    deadline = time.monotonic() + arguments.run_deadline

    versions = {label: info["commit"] for label, info in provenance["versions"].items()}
    runs: list[dict] = []
    cleanup_log: list[dict] = []
    executed: dict[str, dict] = {}
    traces: dict[str, dict] = {}
    expected_hashes: dict[str, str] = {}

    for repetition in range(1, arguments.repetitions + 1):
        for label, commit in versions.items():
            cleanup: dict[str, object] = {}
            with tempfile.TemporaryDirectory(
                prefix="cursorproof-emdash-", dir=workdir
            ) as temporary:
                scratch = Path(temporary)
                manifest = scratch / "manifest.json" if repetition == 1 else None
                with harness(workdir, commit, scratch, manifest, cleanup) as (port, database):
                    if manifest is not None:
                        executed[label] = json.loads(manifest.read_text())[
                            "executed_upstream_files"
                        ]
                    for name in SCENARIOS:
                        if time.monotonic() > deadline:
                            raise RuntimeError("Run deadline exceeded")
                        config = build_config(name, port, database, scratch / f"{name}.json")
                        trace = scratch / f"{name}-trace.json"
                        checked = subprocess.run(
                            [sys.executable, "-m", "cursorproof", "check", str(config)],
                            stdin=subprocess.DEVNULL,
                            capture_output=True,
                            timeout=60,
                            check=False,
                        )
                        if checked.returncode != 0:
                            raise RuntimeError(f"Invalid configuration: {name}")
                        code, report = cli(["run", str(config), "--repro", str(trace)])
                        replay_code, replay = cli(["replay", str(trace)])
                        signature = report_signature(report)
                        oracle_hash = sha256_text("\n".join(report["trace"]["oracle"]))
                        expected_hashes.setdefault(name, oracle_hash)
                        expectation = (
                            "pass" if label == "after" or name in CONTROLS_BEFORE else "violation"
                        )
                        met = (
                            code == 0 and is_clean(signature)
                            if expectation == "pass"
                            else code == 1
                            and bool(signature["findings"])
                            and not signature["errors"]
                        )
                        runs.append(
                            {
                                "version": label,
                                "repetition": repetition,
                                "scenario": name,
                                "exit_code": code,
                                "oracle_sha256": oracle_hash,
                                "offline_replay_matches": replay_code == code and replay == report,
                                "expectation": expectation,
                                "expectation_met": met,
                                **signature,
                            }
                        )
                        if repetition == 1 and f"{label}:{name}" in arguments.traces:
                            target = output / "traces" / f"{label}-{name}.json"
                            shutil.copyfile(trace, target)
                            traces[target.name] = {
                                "version": label,
                                "scenario": name,
                                "exit_code": code,
                                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                                "signature": signature,
                            }
                        print(
                            f"rep {repetition} {label:6} {name:13} exit={code} "
                            f"findings={signature['findings']} met={met}",
                            flush=True,
                        )
            cleanup_log.append(
                {
                    "version": label,
                    "repetition": repetition,
                    **cleanup,
                    "database_removed": not scratch.exists(),
                }
            )

    # Aggregate: identical signatures across repetitions means the outcome is stable.
    stability: dict[str, dict[str, bool]] = {}
    for label in versions:
        stability[label] = {}
        for name in SCENARIOS:
            signatures = {
                json.dumps(
                    {k: v for k, v in run.items() if k not in {"repetition"}}, sort_keys=True
                )
                for run in runs
                if run["version"] == label and run["scenario"] == name
            }
            stability[label][name] = len(signatures) == 1

    before, after = executed.get("before", {}), executed.get("after", {})
    git_head = tool_version(["git", "-C", str(REPOSITORY_ROOT), "rev-parse", "HEAD"])
    product_diff = tool_version(
        ["git", "-C", str(REPOSITORY_ROOT), "status", "--porcelain", "--", "src"]
    )
    summary = {
        "lab": "emdash-2691",
        "note": "Timestamps and ports are not recorded; outcomes are compared by signature.",
        "cursorproof": {
            "version": tool_version([sys.executable, "-m", "cursorproof", "--version"]),
            "checkout_commit": git_head,
            "src_unchanged_in_checkout": product_diff == "",
        },
        "runtime": {
            "node": tool_version(["node", "--version"]),
            "npm": tool_version(["npm", "--version"]),
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.machine()}",
        },
        "fixture": {
            "sha256": hashlib.sha256((LAB / "fixture.json").read_bytes()).hexdigest(),
            "documents": json.loads((LAB / "fixture.json").read_text())["documents"].__len__(),
            "expected_order_sha256": expected_hashes,
        },
        "versions": versions,
        "executed_upstream_files": {label: len(files) for label, files in executed.items()},
        "executed_files_differing_between_versions": sorted(
            path for path in set(before) | set(after) if before.get(path) != after.get(path)
        ),
        "repetitions": arguments.repetitions,
        "all_expectations_met": all(run["expectation_met"] for run in runs),
        "all_offline_replays_match": all(run["offline_replay_matches"] for run in runs),
        "stable_across_repetitions": stability,
        "cleanup": cleanup_log,
        "traces": traces,
        "runs": runs,
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    (output / "executed-upstream-files.json").write_text(
        json.dumps(executed, indent=1, sort_keys=True) + "\n"
    )
    print(f"Evidence: {output}")
    return 0 if summary["all_expectations_met"] and summary["all_offline_replays_match"] else 1


if __name__ == "__main__":
    sys.exit(main())
