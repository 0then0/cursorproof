import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from cursorproof import __version__
from cursorproof.cli import app
from cursorproof.config import Config, ConfigError, load_config
from cursorproof.paths import extract

runner = CliRunner()


def test_cli_help_and_version() -> None:
    assert runner.invoke(app, ["--help"]).exit_code == 0
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_config_defaults_and_check(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text("url: https://example.test/api\n")
    config, _ = load_config(path)
    assert config.limits == [1, 10, 50, 51, 100]
    assert runner.invoke(app, ["check", str(path)]).exit_code == 0


def test_environment_substitution_cannot_inject_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yml"
    secret = 'a: "quoted"\nlimits: [0]'
    monkeypatch.setenv("TOKEN", secret)
    path.write_text("url: https://api.test\nparameters:\n  value: ${TOKEN}\n")
    config, secrets = load_config(path)
    assert config.parameters["value"] == secret
    assert secret in secrets


def test_missing_env_and_invalid_config_do_not_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yml"
    monkeypatch.delenv("CURSORPROOF_MISSING", raising=False)
    path.write_text("url: https://api.test\nheaders:\n  X: ${CURSORPROOF_MISSING}\n")
    with pytest.raises(ConfigError, match="missing or empty"):
        load_config(path)
    path.write_text("url: https://api.test\nlimits: [secret-value]\n")
    result = runner.invoke(app, ["run", str(path), "--format", "json"])
    assert result.exit_code == 2
    assert "secret-value" not in result.stdout
    assert json.loads(result.stdout)["outcome"] == "error"


@pytest.mark.parametrize(
    "changes",
    [
        {"limits": [0]},
        {"limits": [True]},
        {"limits": [1, 1]},
        {"limit": 1},
        {"url": "file:///tmp/test"},
        {"url": "https://user:pass@api.test"},
        {"parameters": {"cursor": "abc"}},
        {"url": "https://api.test?limit=5"},
        {"pagination": {"cursor_param": "x", "limit_param": "x"}},
        {"response": {"items": "$..results"}},
        {"oracle": {}},
        {"oracle": {"command": []}},
        {"mutations": [{"after_page": 1, "command": ["echo"]}]},
        {"consistency": "snapshot", "limits": [1, 2]},
        {"consistency": "live-keyset"},
    ],
)
def test_invalid_contracts_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"url": "https://api.test", "limits": [1], **changes})


def test_paths_and_missing_fields() -> None:
    assert extract({"results": [{"id": 12}]}, "$.results[0].id") == 12
    assert extract(12, "$") == 12
    with pytest.raises(ValueError):
        extract({}, "$.results")


def test_replay_rejects_unsupported_trace(tmp_path: Path) -> None:
    path = tmp_path / "trace.json"
    path.write_text('{"schema_version": 99}')
    result = runner.invoke(app, ["replay", str(path), "--format", "json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["outcome"] == "error"


@pytest.mark.parametrize(
    "trace_content",
    [
        '{"schema_version":1,"errors":null}',
        '{"schema_version":1,"bindings":null}',
    ],
)
def test_replay_reports_malformed_legacy_fields_safely(tmp_path: Path, trace_content: str) -> None:
    path = tmp_path / "trace.json"
    path.write_text(trace_content)

    result = runner.invoke(app, ["replay", str(path), "--format", "json"])

    assert result.exit_code == 2
    assert json.loads(result.stdout)["outcome"] == "error"


def test_replay_reports_deeply_nested_json_safely(tmp_path: Path) -> None:
    path = tmp_path / "trace.json"
    path.write_text("[" * 2000 + "0" + "]" * 2000)

    result = runner.invoke(app, ["replay", str(path), "--format", "json"])

    assert result.exit_code == 2
    assert json.loads(result.stdout)["outcome"] == "error"


def legacy_trace(consistency: str = "static", bindings: list[dict[str, object]] | None = None):
    return {
        "schema_version": 1,
        "tool_version": "0.1.0",
        "consistency": consistency,
        "ordering_fields": [],
        "traversals": [
            {
                "limit": 1,
                "repetition": 1,
                "pages": [
                    {
                        "number": 1,
                        "request": "https://api.test/orders?limit=1",
                        "cursor": None,
                        "next_cursor": None,
                        "has_more": None,
                        "status": 200,
                        "items": [{"id": 1, "sort_key": None}],
                    }
                ],
                "stop": "terminal",
            }
        ],
        "oracle": None,
        "oracle_ordered": True,
        "bindings": bindings or [],
        "mutations": [],
        "errors": [],
        "notes": [],
    }


def test_legacy_static_trace_is_migrated_to_schema_two(tmp_path: Path) -> None:
    path = tmp_path / "legacy-static.json"
    path.write_text(json.dumps(legacy_trace()))
    result = runner.invoke(app, ["replay", str(path), "--format", "json"])
    report = json.loads(result.stdout)
    assert result.exit_code == 0
    assert report["trace"]["schema_version"] == 2


def test_legacy_snapshot_trace_is_incomplete_not_pass(tmp_path: Path) -> None:
    path = tmp_path / "legacy-snapshot.json"
    path.write_text(json.dumps(legacy_trace("snapshot")))
    result = runner.invoke(app, ["replay", str(path), "--format", "json"])
    report = json.loads(result.stdout)
    assert result.exit_code == 2
    assert report["outcome"] == "error"


def test_legacy_binding_trace_is_incomplete_not_rejected_as_invalid_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-binding.json"
    binding = {
        "parameter": "status",
        "request": "https://api.test/orders?status=closed&cursor=cursor_1",
        "cursor": "cursor_1",
        "status": 422,
        "accepted_rejection": True,
    }
    path.write_text(json.dumps(legacy_trace(bindings=[binding])))
    result = runner.invoke(app, ["replay", str(path), "--format", "json"])
    report = json.loads(result.stdout)
    assert result.exit_code == 2
    assert report["outcome"] == "error"
    assert any("lacks a successful baseline" in issue["message"] for issue in report["errors"])


def test_trace_cannot_overwrite_config(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    original = "url: https://api.test\n"
    path.write_text(original)
    result = runner.invoke(app, ["run", str(path), "--repro", str(path)])
    assert result.exit_code == 2
    assert path.read_text() == original
