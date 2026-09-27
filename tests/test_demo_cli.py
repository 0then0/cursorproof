import json
import runpy
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner

from cursorproof.cli import app
from cursorproof.config import Config, load_config
from cursorproof.runner import run


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
                broken_config = tmp_path / "broken-config.json"
                broken_config.write_bytes(config.read_bytes())
                live_replay = runner.invoke(
                    app,
                    [
                        "replay",
                        str(broken_trace),
                        "--config",
                        str(broken_config),
                        "--traversal",
                        "1",
                        "--format",
                        "json",
                    ],
                )
                assert live_replay.exit_code == 1
                assert json.loads(live_replay.stdout)["summary"]["traversals"] == 1
                mismatched = json.loads(broken_config.read_text())
                mismatched["url"] = "http://127.0.0.1:1/different"
                broken_config.write_text(json.dumps(mismatched))
                mismatch_replay = runner.invoke(
                    app,
                    [
                        "replay",
                        str(broken_trace),
                        "--config",
                        str(broken_config),
                        "--format",
                        "json",
                    ],
                )
                assert mismatch_replay.exit_code == 2
                assert "does not match" in mismatch_replay.stdout
            else:
                assert report["summary"]["unique_items"] == 40
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    result = runner.invoke(app, ["replay", str(broken_trace), "--format", "json"])
    assert result.exit_code == 1
    assert any(finding["code"] == "CP003" for finding in json.loads(result.stdout)["findings"])


def test_boundary_cli_runs_fixture_scenarios_and_cleans_up(tmp_path: Path) -> None:
    marker = tmp_path / "fixture-count"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            count = int(marker.read_text())
            ids = list(range(count))
            limit = int(self.path.split("limit=")[1].split("&")[0])
            cursor = None
            if "cursor=" in self.path:
                cursor = int(self.path.split("cursor=")[1].split("&")[0])
            offset = cursor or 0
            items = ids[offset : offset + limit]
            next_offset = offset + limit
            next_cursor = str(next_offset) if next_offset < len(ids) else None
            body = json.dumps(
                {"results": [{"id": item} for item in items], "next_cursor": next_cursor}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    config = tmp_path / "boundary.json"
    config.write_text(
        json.dumps(
            {
                "url": f"http://127.0.0.1:{server.server_port}/orders",
                "limits": [2],
                "boundary_testing": {
                    "setup": [
                        sys.executable,
                        "-c",
                        "import sys; from pathlib import Path; "
                        "Path('fixture-count').write_text(sys.argv[1])",
                        "{count}",
                    ],
                    "cleanup": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('fixture-count').unlink(missing_ok=True)",
                    ],
                },
            }
        )
    )
    try:
        result = CliRunner().invoke(app, ["boundary", str(config), "--format", "json"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.exit_code == 0, result.stdout
    report = json.loads(result.stdout)
    assert report["outcome"] == "pass"
    assert [case["expected_items"] for case in report["cases"]] == [0, 1, 2, 3, 4, 5]
    assert not marker.exists()


def test_boundary_failure_saves_trace_that_offline_replay_fails(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    marker = tmp_path / "fixture-count"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            count = int(marker.read_text())
            ids = list(range(count))
            if count == 3:
                ids.remove(0)
            limit = int(self.path.split("limit=")[1].split("&")[0])
            offset = 0
            if "cursor=" in self.path:
                offset = int(self.path.split("cursor=")[1].split("&")[0])
            items = ids[offset : offset + limit]
            next_offset = offset + limit
            next_cursor = str(next_offset) if next_offset < len(ids) else None
            body = json.dumps(
                {"results": [{"id": item} for item in items], "next_cursor": next_cursor}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    config = tmp_path / "boundary.json"
    config.write_text(
        json.dumps(
            {
                "url": f"http://127.0.0.1:{server.server_port}/orders",
                "limits": [2, 3],
                "boundary_testing": {
                    "setup": [
                        sys.executable,
                        "-c",
                        "import sys; from pathlib import Path; "
                        "Path('fixture-count').write_text(sys.argv[1])",
                        "{count}",
                    ],
                    "cleanup": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('fixture-count').unlink(missing_ok=True)",
                    ],
                },
            }
        )
    )
    trace = tmp_path / "boundary-repro.json"
    runner = CliRunner()
    try:
        result = runner.invoke(
            app,
            ["boundary", str(config), "--format", "json", "--repro", str(trace)],
        )
        assert result.exit_code == 1, result.stdout
        report = json.loads(result.stdout)
        assert report["repro_path"] == str(trace)
        assert trace.exists()
        replay = runner.invoke(app, ["replay", str(trace), "--format", "json"])
        assert replay.exit_code == 1, replay.stdout
        replayed = json.loads(replay.stdout)
        assert replayed["findings"][0]["code"] == "CP012"
        assert replayed["trace"]["expected_unique_items"] == 3
        live_without_hooks = runner.invoke(
            app,
            ["replay", str(trace), "--config", str(config), "--format", "json"],
        )
        assert live_without_hooks.exit_code == 2
        live = runner.invoke(
            app,
            [
                "replay",
                str(trace),
                "--config",
                str(config),
                "--execute-hooks",
                "--format",
                "json",
            ],
        )
        assert live.exit_code == 1, live.stdout
        live_report = json.loads(live.stdout)
        assert live_report["findings"][0]["code"] == "CP012"
        assert not marker.exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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


def test_live_replay_rejects_changed_check_contract(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"results": [{"id": 2}, {"id": 1}], "next_cursor": None},
        )
    )
    original = Config.model_validate(
        {
            "url": "https://api.test/orders",
            "limits": [2],
            "ordering": [{"field": "id", "direction": "asc"}],
        }
    )
    original_report = run(original, transport=transport)
    assert "CP004" in {finding.code for finding in original_report.findings}
    trace = tmp_path / "trace.json"
    trace.write_text(original_report.trace.model_dump_json())
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"url": "https://api.test/orders", "limits": [2], "ordering": []}))

    with patch(
        "cursorproof.cli.execute",
        side_effect=lambda parsed, **kwargs: run(parsed, transport=transport),
    ) as execute:
        result = CliRunner().invoke(
            app, ["replay", str(trace), "--config", str(config), "--format", "json"]
        )

    assert result.exit_code == 2
    assert "contract does not match" in result.stdout
    execute.assert_not_called()


def test_live_replay_rejects_changed_env_stream_parameter(tmp_path: Path, monkeypatch) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "results": [
                    {"id": item}
                    for item in ([2, 1] if request.url.params["tenant"] == "tenant-a" else [1, 2])
                ],
                "next_cursor": None,
            },
        )
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "url": "https://api.test/orders",
                "limits": [2],
                "parameters": {"tenant": "${CURSORPROOF_TENANT}"},
                "ordering": [{"field": "id", "direction": "asc"}],
            }
        )
    )
    monkeypatch.setenv("CURSORPROOF_TENANT", "tenant-a")
    original, secrets = load_config(config_path)
    original_report = run(original, secrets=secrets, transport=transport)
    assert "CP004" in {finding.code for finding in original_report.findings}
    trace = tmp_path / "tenant-trace.json"
    trace.write_text(original_report.trace.model_dump_json())
    assert "tenant-a" not in trace.read_text()

    monkeypatch.setenv("CURSORPROOF_TENANT", "tenant-b")
    with patch(
        "cursorproof.cli.execute",
        side_effect=lambda parsed, **kwargs: run(
            parsed, secrets=kwargs["secrets"], transport=transport
        ),
    ) as execute:
        result = CliRunner().invoke(
            app,
            ["replay", str(trace), "--config", str(config_path), "--format", "json"],
        )

    assert result.exit_code == 2
    assert "contract does not match" in result.stdout
    execute.assert_not_called()


def test_live_replay_rejects_changed_author_filter(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "results": [
                    {"id": item}
                    for item in ([2, 1] if request.url.params["author"] == "alice" else [1, 2])
                ],
                "next_cursor": None,
            },
        )
    )
    original = Config.model_validate(
        {
            "url": "https://api.test/orders?author=alice",
            "limits": [2],
            "ordering": [{"field": "id", "direction": "asc"}],
        }
    )
    original_report = run(original, transport=transport)
    assert "CP004" in {finding.code for finding in original_report.findings}
    trace = tmp_path / "trace.json"
    trace.write_text(original_report.trace.model_dump_json())
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "url": "https://api.test/orders?author=bob",
                "limits": [2],
                "ordering": [{"field": "id", "direction": "asc"}],
            }
        )
    )

    with patch(
        "cursorproof.cli.execute",
        side_effect=lambda parsed, **kwargs: run(parsed, transport=transport),
    ) as execute:
        result = CliRunner().invoke(
            app, ["replay", str(trace), "--config", str(config), "--format", "json"]
        )

    assert result.exit_code == 2
    assert "contract does not match" in result.stdout
    execute.assert_not_called()


def test_live_replay_client_setup_error_is_safe_json(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"url": "https://api.test/orders", "limits": [1]}))
    original = Config(url="https://api.test/orders", limits=[1])
    report = run(
        original,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"results": [], "next_cursor": None})
        ),
    )
    trace = tmp_path / "trace.json"
    trace.write_text(report.trace.model_dump_json())
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing-certificate.pem"))

    result = CliRunner().invoke(
        app,
        ["replay", str(trace), "--config", str(config_path), "--format", "json"],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["outcome"] == "error"
    assert "missing-certificate" not in result.stdout


def test_check_never_executes_config_commands(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "url": "http://127.0.0.1:1/",
                "limits": [1],
                "consistency": "snapshot",
                "response": {"snapshot_fields": ["$"]},
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


def test_live_replay_requires_opt_in_before_mutation_hooks(tmp_path: Path) -> None:
    from cursorproof.models import Trace, Traversal

    saved_trace = tmp_path / "trace.json"
    saved_trace.write_text(
        Trace(
            tool_version="test",
            consistency="snapshot",
            traversals=[Traversal(limit=5, stop="terminal")],
        ).model_dump_json()
    )
    marker = tmp_path / "mutation-ran"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "url": "http://127.0.0.1:1/orders",
                "limits": [5],
                "consistency": "snapshot",
                "response": {"snapshot_fields": ["$"]},
                "oracle": {"command": [sys.executable, "-c", "print('[]')"]},
                "mutations": [
                    {
                        "after_page": 1,
                        "command": [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
                    }
                ],
            }
        )
    )
    result = CliRunner().invoke(app, ["replay", str(saved_trace), "--config", str(config)])
    assert result.exit_code == 2
    assert not marker.exists()


def test_live_replay_requires_opt_in_before_oracle_command(tmp_path: Path) -> None:
    from cursorproof.models import Trace, Traversal

    saved_trace = tmp_path / "trace.json"
    saved_trace.write_text(
        Trace(
            tool_version="test",
            consistency="static",
            traversals=[Traversal(limit=5, stop="terminal")],
        ).model_dump_json()
    )
    marker = tmp_path / "oracle-ran"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "url": "http://127.0.0.1:1/orders",
                "limits": [5],
                "oracle": {
                    "command": [
                        sys.executable,
                        "-c",
                        f"open({str(marker)!r}, 'w').close(); print('[]')",
                    ]
                },
            }
        )
    )
    result = CliRunner().invoke(app, ["replay", str(saved_trace), "--config", str(config)])
    assert result.exit_code == 2
    assert not marker.exists()


def test_live_replay_rejects_ambiguous_literal_oracle_argument(tmp_path: Path, monkeypatch) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"results":[{"id":1}],"next_cursor":null}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("PIN", "abc")
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    config = tmp_path / "config.json"
    trace = tmp_path / "trace.json"
    marker = tmp_path / "oracle-ran"
    config.write_text(
        json.dumps(
            {
                "url": f"http://127.0.0.1:{server.server_port}/orders",
                "limits": [1],
                "oracle": {
                    "command": [
                        sys.executable,
                        "-c",
                        f"open({str(marker)!r}, 'w').close(); print('[1]')",
                        "--pin=${PIN}",
                        "token=abc",
                    ]
                },
            }
        )
    )
    try:
        initial = CliRunner().invoke(
            app, ["run", str(config), "--format", "json", "--repro", str(trace)]
        )
        assert initial.exit_code == 0, initial.output
        assert json.loads(trace.read_text())["replay_fingerprint"] is None

        marker.unlink()
        monkeypatch.setenv("PIN", "def")
        updated_config = json.loads(config.read_text())
        updated_config["oracle"]["command"][-1] = "token=def"
        config.write_text(json.dumps(updated_config))
        replay = CliRunner().invoke(
            app,
            [
                "replay",
                str(trace),
                "--config",
                str(config),
                "--execute-hooks",
                "--format",
                "json",
            ],
        )
        assert replay.exit_code == 2
        assert "lacks replay contract evidence" in replay.stdout
        assert not marker.exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("old_dataset", "new_dataset"),
    [
        ("longdataset", "short"),
        ("tenant-abc", "tenant-def"),
        ("tenant-token=abc", "tenant-token=def"),
        ("token=abc", "token=def"),
    ],
)
@pytest.mark.parametrize("with_dataset_option", [False, True])
def test_live_replay_rejects_changed_oracle_dataset_environment(
    tmp_path: Path,
    monkeypatch,
    old_dataset: str,
    new_dataset: str,
    with_dataset_option: bool,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b'{"results":[{"id":1}],"next_cursor":null}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    config = tmp_path / "config.json"
    trace = tmp_path / "trace.json"
    dataset_position = 2 if with_dataset_option else 1
    dataset_arguments = (["--dataset"] if with_dataset_option else []) + ["${DATASET}"]
    monkeypatch.setenv("DATASET", old_dataset)
    monkeypatch.setenv("ORACLE_PIN", "abc")
    config.write_text(
        json.dumps(
            {
                "url": f"http://127.0.0.1:{server.server_port}/orders",
                "limits": [1],
                "oracle": {
                    "command": [
                        sys.executable,
                        "-c",
                        f"import json,sys; print(json.dumps([1,2] if "
                        f"len(sys.argv[{dataset_position}]) > 5 else [1]))",
                        *dataset_arguments,
                        "--pin=${ORACLE_PIN}",
                    ]
                },
            }
        )
    )
    try:
        initial = CliRunner().invoke(
            app, ["run", str(config), "--format", "json", "--repro", str(trace)]
        )
        assert initial.exit_code == 1
        assert any(finding["code"] == "CP003" for finding in json.loads(initial.stdout)["findings"])

        monkeypatch.setenv("DATASET", new_dataset)
        monkeypatch.setenv("ORACLE_PIN", "def")
        replay = CliRunner().invoke(
            app,
            [
                "replay",
                str(trace),
                "--config",
                str(config),
                "--execute-hooks",
                "--format",
                "json",
            ],
        )
        assert replay.exit_code == 2
        assert "contract does not match" in replay.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_live_replay_accepts_rotated_pin_despite_display_url_redaction(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    trace = tmp_path / "trace.json"
    config.write_text(
        json.dumps(
            {
                "url": "http://127.0.0.1:8000/orders",
                "limits": [1],
                "oracle": {"command": [sys.executable, "-c", "print('[]')", "--pin=${PIN}"]},
            }
        )
    )
    monkeypatch.setenv("PIN", "12")
    parsed, secrets = load_config(config)
    report = run(
        parsed,
        secrets=secrets,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"results": [], "next_cursor": None})
        ),
    )
    assert "[REDACTED]7.0.0.1" in report.trace.traversals[0].pages[0].request
    trace.write_text(report.trace.model_dump_json())
    monkeypatch.setenv("PIN", "34")
    with patch("cursorproof.cli.execute", return_value=report) as execute:
        result = CliRunner().invoke(
            app, ["replay", str(trace), "--config", str(config), "--execute-hooks"]
        )
    assert result.exit_code == 0, result.stdout
    execute.assert_called_once()


@pytest.mark.parametrize(
    "auth",
    [
        {"headers": {"Authorization": "Bearer old"}},
        {"headers": {"Cookie": "session=old"}},
        {"parameters": {"api_key": "old"}},
        {"url": "https://api.test/orders?token=old"},
        {"oracle": {"url": "https://api.test/oracle", "headers": {"Authorization": "Bearer old"}}},
        {"oracle": {"url": "https://api.test/oracle?token=old"}},
    ],
)
def test_live_replay_requires_explicit_auth_scope_acceptance(tmp_path, auth):
    config = tmp_path / "config.json"
    trace = tmp_path / "trace.json"
    values = {"url": "https://api.test/orders", "limits": [1], **auth}
    parsed = Config.model_validate(values)
    report = run(
        parsed,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=[] if request.url.path == "/oracle" else {"results": [], "next_cursor": None},
            )
        ),
    )
    assert report.exit_code == 0
    trace.write_text(report.trace.model_dump_json())
    # Rotated credentials may select another principal, despite matching fingerprints.
    config.write_text(json.dumps(values).replace("old", "new"))
    args = ["replay", str(trace), "--config", str(config), "--format", "json"]
    with patch("cursorproof.cli.execute", return_value=report) as execute:
        rejected = CliRunner().invoke(app, args)
        assert rejected.exit_code == 2, rejected.stdout
        assert "--allow-unverified-auth-scope" in rejected.stdout
        execute.assert_not_called()
        accepted = CliRunner().invoke(app, [*args, "--allow-unverified-auth-scope"])
        assert accepted.exit_code == 0, accepted.stdout
        execute.assert_called_once()
        assert any(
            "scope were not verified" in note for note in json.loads(accepted.stdout)["notes"]
        )
    offline = CliRunner().invoke(app, ["replay", str(trace), "--allow-unverified-auth-scope"])
    assert offline.exit_code == 2
