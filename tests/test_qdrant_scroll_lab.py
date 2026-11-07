"""Committed evidence from the Qdrant Scroll lab; the live run needs Docker (see the lab docs)."""

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cursorproof.cli import app
from cursorproof.config import load_config
from cursorproof.models import parse_trace
from cursorproof.privacy import replay_fingerprint

LAB = Path(__file__).parents[1] / "labs" / "qdrant-scroll"
EXPECTED_EXIT = {"ints": 0, "uuids": 0, "ints-without-filter": 1}


@pytest.fixture
def fixture_module() -> object:
    sys.path.insert(0, str(LAB))
    try:
        import fixture  # type: ignore[import-not-found]

        yield fixture
    finally:
        sys.path.remove(str(LAB))
        sys.modules.pop("fixture", None)


def test_summary_records_the_pinned_version_and_commands() -> None:
    summary = json.loads((LAB / "evidence" / "summary.json").read_text())
    assert summary["passed"] is True
    assert summary["qdrant"]["version"] == "1.20.0"
    assert summary["image"] == "qdrant/qdrant:v1.20.0"
    assert summary["image_digest"].startswith("qdrant/qdrant@sha256:")
    assert any(command.startswith("docker run") for command in summary["commands"])


@pytest.mark.parametrize("name", sorted(EXPECTED_EXIT))
def test_saved_traces_replay_offline_and_match_their_configurations(name: str) -> None:
    trace_path = LAB / "evidence" / "traces" / f"{name}.json"
    trace = parse_trace(trace_path.read_bytes())
    assert trace.request_method == "POST"
    config, secrets = load_config(LAB / "configs" / f"{name}.yml")
    assert replay_fingerprint(config, secrets) == trace.replay_fingerprint
    result = CliRunner().invoke(app, ["replay", str(trace_path), "--format", "json"])
    assert result.exit_code == EXPECTED_EXIT[name], result.output
    report = json.loads(result.stdout)
    if name == "ints-without-filter":
        assert {finding["code"] for finding in report["findings"]} == {"CP010"}
    else:
        assert report["summary"]["unique_items"] == 30


def test_traces_hold_exact_integer_and_uuid_ids(fixture_module: object) -> None:
    for name, expected in (
        ("ints", fixture_module.expected_ints()),  # type: ignore[attr-defined]
        ("uuids", fixture_module.expected_uuids()),  # type: ignore[attr-defined]
    ):
        trace = parse_trace((LAB / "evidence" / "traces" / f"{name}.json").read_bytes())
        for traversal in trace.traversals:
            stream = [item.id for page in traversal.pages for item in page.items]
            assert stream == expected
            assert traversal.stop == "terminal"
    assert 2**64 - 2 in fixture_module.expected_ints()  # type: ignore[attr-defined]
