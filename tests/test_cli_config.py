import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from cursorproof.cli import app
from cursorproof.config import Config, ConfigError, load_config
from cursorproof.paths import extract

runner = CliRunner()


def test_cli_help_and_version() -> None:
    assert runner.invoke(app, ["--help"]).exit_code == 0
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout


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


def test_trace_cannot_overwrite_config(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    original = "url: https://api.test\n"
    path.write_text(original)
    result = runner.invoke(app, ["run", str(path), "--repro", str(path)])
    assert result.exit_code == 2
    assert path.read_text() == original
