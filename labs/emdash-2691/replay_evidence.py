#!/usr/bin/env python3
"""Replay the saved EmDash #2691 traces offline and compare them with the recorded results.

Needs only CursorProof and the evidence directory: no harness, no Node.js, no upstream source and no
database. Replay re-analyses the recorded observations; it does not execute EmDash again.

    python replay_evidence.py [--evidence evidence]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from lab_metrics import report_signature

DEFAULT_EVIDENCE = Path(__file__).resolve().parent / "evidence"


def replay(trace: Path) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, "-m", "cursorproof", "replay", str(trace), "--format", "json"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode not in {0, 1, 2}:
        raise RuntimeError(f"CursorProof replay failed: {result.stderr[-2000:]}")
    return result.returncode, json.loads(result.stdout)


def verify(evidence: Path) -> list[dict]:
    summary = json.loads((evidence / "summary.json").read_text())
    results = []
    for name, recorded in sorted(summary["traces"].items()):
        path = evidence / "traces" / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        code, report = replay(path)
        signature = report_signature(report)
        results.append(
            {
                "trace": name,
                "version": recorded["version"],
                "scenario": recorded["scenario"],
                "exit_code": code,
                "findings": signature["findings"],
                "digest_matches": digest == recorded["sha256"],
                "matches_recorded_run": code == recorded["exit_code"]
                and signature == recorded["signature"],
            }
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    arguments = parser.parse_args()
    results = verify(arguments.evidence)
    for result in results:
        print(
            f"{result['trace']:28} exit={result['exit_code']} findings={result['findings']} "
            f"digest={result['digest_matches']} matches_run={result['matches_recorded_run']}"
        )
    ok = bool(results) and all(r["digest_matches"] and r["matches_recorded_run"] for r in results)
    print("Offline replay matches the recorded runs" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
