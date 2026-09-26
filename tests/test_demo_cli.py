import json
import runpy
import sys
from http.server import HTTPServer
from pathlib import Path
from threading import Thread

from typer.testing import CliRunner

from cursorproof.cli import app


def test_demo_run_and_offline_replay(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    handler = runpy.run_path(str(Path(__file__).parents[1] / "examples" / "demo_api.py"))["Handler"]
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    runner = CliRunner()
    trace = tmp_path / "repro.json"
    base = f"http://127.0.0.1:{server.server_port}"
    config = tmp_path / "config.json"
    try:
        for endpoint, expected_exit in [("broken", 1), ("correct", 0)]:
            config.write_text(
                json.dumps(
                    {
                        "url": f"{base}/{endpoint}",
                        "limits": [5, 6],
                        "oracle": {"url": f"{base}/oracle"},
                        "ordering": [
                            {"field": "created_at", "direction": "desc"},
                            {"field": "id", "direction": "desc"},
                        ],
                    }
                )
            )
            result = runner.invoke(
                app, ["run", str(config), "--format", "json", "--repro", str(trace)]
            )
            assert result.exit_code == expected_exit, result.output
            report = json.loads(result.stdout)
            assert all(
                page["status"] == 200
                for traversal in report["trace"]["traversals"]
                for page in traversal["pages"]
            )
            if endpoint == "broken":
                missing = [finding for finding in report["findings"] if finding["code"] == "CP003"]
                assert missing
                assert any(len(finding["locations"]) == 2 for finding in missing)
                broken_trace = tmp_path / "broken.json"
                broken_trace.write_bytes(trace.read_bytes())
            else:
                assert report["summary"]["unique_items"] == 40
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    result = runner.invoke(app, ["replay", str(broken_trace), "--format", "json"])
    assert result.exit_code == 1
    assert any(finding["code"] == "CP003" for finding in json.loads(result.stdout)["findings"])


def test_cli_client_setup_error_is_safe_json(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"url": "https://api.test", "limits": [1]}))
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "secret-nonexistent-certificate"))
    result = CliRunner().invoke(
        app,
        [
            "run",
            str(config),
            "--format",
            "json",
            "--repro",
            str(tmp_path / "failed.json"),
        ],
    )
    assert result.exit_code == 2
    assert "secret-nonexistent" not in result.stdout
    assert json.loads(result.stdout)["outcome"] == "error"


def test_check_never_executes_config_commands(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "url": "http://127.0.0.1:1/",
                "limits": [1],
                "consistency": "snapshot",
                "oracle": {
                    "command": [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
                },
                "mutations": [{"after_page": 1, "command": ["not-a-command"]}],
            }
        )
    )
    result = CliRunner().invoke(app, ["check", str(config)])
    assert result.exit_code == 0
    assert not marker.exists()
