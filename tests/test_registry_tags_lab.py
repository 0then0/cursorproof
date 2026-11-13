"""Committed evidence from the Registry tags lab; the live run needs Docker (see the lab docs)."""

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cursorproof.cli import app
from cursorproof.config import load_config
from cursorproof.models import parse_trace
from cursorproof.privacy import replay_fingerprint

LAB = Path(__file__).parents[1] / "labs" / "registry-tags"
EXPECTED_EXIT = {"tags": 0, "tags-partial": 1}


@pytest.fixture
def fixture_module() -> object:
    sys.path.insert(0, str(LAB))
    try:
        import fixture  # type: ignore[import-not-found]

        yield fixture
    finally:
        sys.path.remove(str(LAB))
        sys.modules.pop("fixture", None)


def test_summary_records_the_pinned_image_api_contract_and_cleanup() -> None:
    summary = json.loads((LAB / "evidence" / "summary.json").read_text())
    assert summary["passed"] is True
    assert summary["image"].startswith("registry:3.0.0@sha256:")
    assert summary["registry_version"].endswith("v3 3.0.0")
    assert summary["container_removed"] is True
    assert summary["container_mounts"] == []
    first, final = summary["api_observations"]
    assert first["link"] == ['</v2/cursorproof/tags/tags/list?last=10&n=5>; rel="next"']
    assert final["link"] == [] and final["body"]["tags"] == ["zeta"]


@pytest.mark.parametrize("name", sorted(EXPECTED_EXIT))
def test_saved_traces_replay_offline_and_match_their_configurations(name: str) -> None:
    trace_path = LAB / "evidence" / "traces" / f"{name}.json"
    trace = parse_trace(trace_path.read_bytes())
    assert trace.continuation_source == "link-header"
    assert trace.request_method == "GET"
    config, secrets = load_config(LAB / "configs" / f"{name}.yml")
    assert replay_fingerprint(config, secrets) == trace.replay_fingerprint
    result = CliRunner().invoke(app, ["replay", str(trace_path), "--format", "json"])
    assert result.exit_code == EXPECTED_EXIT[name], result.output
    report = json.loads(result.stdout)
    assert not report["errors"]
    if name == "tags-partial":
        assert {finding["code"] for finding in report["findings"]} == {"CP003"}
        missing = {item for finding in report["findings"] for item in finding["item_ids"]}
        assert missing == {"1.2", "alpha.1", "zeta"}
    else:
        assert report["summary"]["unique_items"] == 24


def test_trace_holds_the_pushed_tags_and_link_evidence(fixture_module: object) -> None:
    expected = fixture_module.expected_tags()  # type: ignore[attr-defined]
    assert sorted(fixture_module.TAGS) == expected  # type: ignore[attr-defined]
    assert fixture_module.TAGS != expected  # type: ignore[attr-defined]
    trace = parse_trace((LAB / "evidence" / "traces" / "tags.json").read_bytes())
    assert trace.oracle == expected
    assert [traversal.limit for traversal in trace.traversals] == [1, 5, 8, 24, 100]
    assert [len(traversal.pages) for traversal in trace.traversals] == [24, 5, 3, 1, 1]
    for traversal in trace.traversals:
        assert [item.id for page in traversal.pages for item in page.items] == expected
        assert traversal.stop == "terminal"
        for page in traversal.pages:
            assert page.link is not None
            assert (page.link.next_links == 1) == (page.next_cursor is not None)
        assert traversal.pages[-1].link.field_lines == 0  # type: ignore[union-attr]
    # Continuations are aliases; the raw `last` values of the Link targets are not stored.
    assert "last=cursor_" in trace.traversals[1].pages[1].request
