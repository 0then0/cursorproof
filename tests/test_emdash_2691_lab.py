"""Offline checks for the EmDash #2691 lab: fixture, oracle and the committed evidence.

They need neither Node.js nor the network. Re-running the lab itself is described in
docs/emdash-2691.md.
"""

import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

LAB = Path(__file__).resolve().parents[1] / "labs" / "emdash-2691"
EVIDENCE = LAB / "evidence"


def load(name: str):
    sys.path.insert(0, str(LAB))
    try:
        spec = importlib.util.spec_from_file_location(name, LAB / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(LAB))


def build_database(path: Path, fixture: dict, created_at: dict[str, str] | None = None) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE _plugin_storage (plugin_id TEXT, collection TEXT, id TEXT, data TEXT, "
        "created_at TEXT, PRIMARY KEY (plugin_id, collection, id))"
    )
    for index, document in enumerate(fixture["documents"]):
        stamp = (created_at or {}).get(document["id"], f"2026-01-01T00:00:{index:02d}.000Z")
        connection.execute(
            "INSERT INTO _plugin_storage VALUES (?, ?, ?, ?, ?)",
            (
                fixture["plugin_id"],
                fixture["collection"],
                document["id"],
                json.dumps(document["data"]),
                stamp,
            ),
        )
    connection.commit()
    connection.close()


def run_oracle(database: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(LAB / "oracle.py"),
            "--db",
            str(database),
            "--fixture",
            str(LAB / "fixture.json"),
            *arguments,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_fixture_file_matches_generator_and_shape() -> None:
    fixture = load("fixture")
    assert (LAB / "fixture.json").read_text() == fixture.render()
    documents = json.loads(fixture.render())["documents"]
    assert [d["id"] for d in documents] == [f"sub-{i:03d}" for i in range(1, 151)]
    scores = [d["data"]["score"] for d in documents]
    # Every score value is shared by exactly three records, so tie groups straddle each page edge.
    assert sorted(set(scores)) == list(range(50)) and all(scores.count(v) == 3 for v in range(50))
    created = [d["data"]["createdAt"] for d in documents]
    assert created == sorted(created) and len(set(created)) == 150
    assert sum(d["data"]["note"] is None for d in documents) == 30


def test_oracle_rules_on_a_hand_computed_case() -> None:
    oracle = load("oracle")
    rows = {
        "a": ("t1", {"v": 1}),
        "b": ("t2", {"v": 1}),
        "c": ("t3", {"v": None}),
        "d": ("t4", {"v": 0}),
        "e": ("t5", {}),
        "f": ("t0", {"v": 10}),
    }
    # Numbers compare numerically (10 after 1), ties break by id, null/missing rank last ascending.
    assert oracle.expected_order(rows, "v", "asc") == ["d", "a", "b", "f", "c", "e"]
    # Descending reverses everything: nulls first, ties by id descending.
    assert oracle.expected_order(rows, "v", "desc") == ["e", "c", "f", "b", "a", "d"]
    assert oracle.expected_order(rows, None, "asc") == ["f", "a", "b", "c", "d", "e"]


def test_oracle_reads_database_and_rejects_data_that_differs_from_fixture(tmp_path: Path) -> None:
    fixture = json.loads((LAB / "fixture.json").read_text())
    database = tmp_path / "db.sqlite3"
    build_database(database, fixture)
    ordered = run_oracle(database, "--sort", "createdAt", "--direction", "desc")
    assert ordered.returncode == 0
    assert ordered.stdout.split() == [f"sub-{i:03d}" for i in range(150, 0, -1)]
    # The default order is the stored (created_at, id) order, not the sort field.
    shuffled = {f"sub-{i:03d}": f"2026-01-02T00:00:{i % 60:02d}.{i:03d}Z" for i in range(1, 151)}
    other = tmp_path / "other.sqlite3"
    build_database(other, fixture, shuffled)
    default = run_oracle(other).stdout.split()
    assert default == sorted(shuffled, key=lambda row_id: (shuffled[row_id], row_id))
    # A database that no longer holds exactly the fixture is refused.
    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM _plugin_storage WHERE id = 'sub-001'")
    connection.commit()
    connection.close()
    refused = run_oracle(database, "--sort", "score")
    assert refused.returncode == 3 and refused.stdout == ""


def test_provenance_pins_full_commits_and_dependencies() -> None:
    provenance = json.loads((LAB / "provenance.json").read_text())
    commits = [version["commit"] for version in provenance["versions"].values()]
    assert len(commits) == 2 and all(re.fullmatch(r"[0-9a-f]{40}", c) for c in commits)
    assert set(provenance["snapshots"]) == set(commits)
    for snapshot in provenance["snapshots"].values():
        assert re.fullmatch(r"[0-9a-f]{64}", snapshot["source_tree_sha256"])
    package = json.loads((LAB / "harness/package.json").read_text())
    lock = json.loads((LAB / "harness/package-lock.json").read_text())
    for name, version in package["dependencies"].items():
        assert re.fullmatch(r"\d+\.\d+\.\d+", version)
        assert lock["packages"][f"node_modules/{name}"]["version"] == version


def test_committed_evidence_shows_the_violation_then_the_fix() -> None:
    summary = json.loads((EVIDENCE / "summary.json").read_text())
    assert summary["repetitions"] >= 3
    assert summary["all_expectations_met"] and summary["all_offline_replays_match"]
    assert summary["cursorproof"]["src_unchanged_in_checkout"]
    assert summary["executed_files_differing_between_versions"] == [
        "packages/core/src/database/repositories/plugin-storage.ts"
    ]
    assert all(
        entry["process_exited"] and entry["port_closed"] and entry["database_removed"]
        for entry in summary["cleanup"]
    )
    assert all(all(flags.values()) for flags in summary["stable_across_repetitions"].values())

    def runs(version: str, scenario: str) -> list[dict]:
        return [r for r in summary["runs"] if (r["version"], r["scenario"]) == (version, scenario)]

    for scenario in ("created-desc", "score-asc", "score-desc", "note-asc", "note-desc"):
        assert len(runs("before", scenario)) == summary["repetitions"]
        for run in runs("before", scenario):
            assert run["exit_code"] == 1 and {"CP002", "CP003"} <= set(run["findings"])
            assert not any(t["exact_order"] for t in run["traversals"])
    for scenario in ("default-order", "created-asc"):
        for run in runs("before", scenario):
            assert run["exit_code"] == 0 and not run["findings"]
    for run in (r for r in summary["runs"] if r["version"] == "after"):
        assert run["exit_code"] == 0 and not run["findings"] and not run["errors"]
        for traversal in run["traversals"]:
            assert traversal["exact_order"] and traversal["duplicates"] == 0
            assert traversal["missing"] == 0 and traversal["observed"] == 150

    # The issue reported 150 records exported as 199 rows with the 50 oldest missing.
    traversals = {t["limit"]: t for t in runs("before", "created-desc")[0]["traversals"]}
    assert (traversals[100]["observed"], traversals[100]["duplicates"]) == (199, 99)
    assert (traversals[100]["unique"], traversals[100]["missing"]) == (100, 50)


def test_saved_traces_replay_offline_to_the_recorded_results() -> None:
    replay = load("replay_evidence")
    results = replay.verify(EVIDENCE)
    assert {r["trace"] for r in results} == {
        "after-created-desc.json",
        "after-score-desc.json",
        "before-created-desc.json",
        "before-default-order.json",
        "before-score-desc.json",
    }
    for result in results:
        assert result["digest_matches"] and result["matches_recorded_run"], result
        expected_exit = (
            1 if result["version"] == "before" and result["scenario"] != "default-order" else 0
        )
        assert result["exit_code"] == expected_exit


@pytest.mark.parametrize(
    "script", ["prepare.py", "run_lab.py", "harness_check.py", "replay_evidence.py"]
)
def test_lab_scripts_document_their_usage(script: str) -> None:
    result = subprocess.run(
        [sys.executable, str(LAB / script), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0 and "usage" in result.stdout.lower()
