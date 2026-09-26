import sys
from pathlib import Path

import httpx
import pytest

from cursorproof.config import Config
from cursorproof.runner import ExecutionError, command_output, run


def config(**changes: object) -> Config:
    return Config.model_validate({"url": "https://api.test/orders", "limits": [2], **changes})


def test_invalid_oracle_idna_url_is_a_safe_error() -> None:
    report = run(
        config(oracle={"url": "https://💩.example"}),
        transport=httpx.MockTransport(lambda request: pytest.fail("Must not send HTTP")),
    )
    assert report.exit_code == 2
    assert "💩" not in report.model_dump_json()


@pytest.mark.parametrize(
    ("output_format", "stdout", "ids"),
    [
        ("json", '[1, "2"]', [1, "2"]),
        ("lines", "one\ntwo\n", ["one", "two"]),
        ("lines", "", []),
    ],
)
def test_command_oracle_formats(
    output_format: str, stdout: str, ids: list[str | int], tmp_path: Path
) -> None:
    report = run(
        config(
            oracle={
                "command": [sys.executable, "-c", f"import sys; sys.stdout.write({stdout!r})"],
                "format": output_format,
            }
        ),
        cwd=tmp_path,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "results": [{"id": item} for item in ids],
                    "next_cursor": None,
                },
            )
        ),
    )
    assert report.exit_code == 0
    assert report.trace.oracle == ids


@pytest.mark.parametrize(
    "command",
    [
        ["cursorproof-command-that-does-not-exist"],
        [sys.executable, "-c", "raise SystemExit(1)"],
        [sys.executable, "-c", "import time; time.sleep(5)"],
    ],
)
def test_oracle_failure_is_incomplete_and_stops_before_http(
    command: list[str], tmp_path: Path
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("HTTP should not run after oracle failure")

    report = run(
        config(oracle={"command": command, "timeout": 0.05}),
        cwd=tmp_path,
        transport=httpx.MockTransport(handle),
    )
    assert report.exit_code == 2
    assert not report.findings


def test_oracle_command_output_is_stopped_at_byte_limit(tmp_path: Path) -> None:
    command = [
        sys.executable,
        "-c",
        "import sys,time\n"
        "while True:\n"
        " sys.stdout.write('x'*4096)\n"
        " sys.stdout.flush()\n"
        " time.sleep(.001)\n",
    ]
    with pytest.raises(ExecutionError, match="exceeded max_response_bytes"):
        command_output(command, timeout=5, cwd=tmp_path, max_bytes=1024)


@pytest.mark.parametrize(
    "command",
    [
        ["cursorproof-command-that-does-not-exist"],
        [sys.executable, "-c", "raise SystemExit(1)"],
        [sys.executable, "-c", "import time; time.sleep(5)"],
    ],
)
def test_mutation_failure_stops_before_next_page(command: list[str], tmp_path: Path) -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": [{"id": 1, "value": "a"}], "next_cursor": "A"})

    report = run(
        config(
            consistency="snapshot",
            response={"snapshot_fields": ["$"]},
            oracle={"command": [sys.executable, "-c", "print('[]')"]},
            mutations=[
                {
                    "after_page": 1,
                    "command": command,
                    "timeout": 0.05,
                }
            ],
        ),
        cwd=tmp_path,
        transport=httpx.MockTransport(handle),
    )
    assert report.exit_code == 2
    assert len(requests) == 1
    assert not report.trace.mutations


@pytest.mark.parametrize(("snapshot", "expected_exit"), [(True, 0), (False, 1)])
def test_snapshot_oracle_detects_visibility_changes(
    snapshot: bool, expected_exit: int, tmp_path: Path
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oracle":
            return httpx.Response(200, json=[{"id": 1, "value": "a"}, {"id": 2, "value": "b"}])
        if "cursor" not in request.url.params:
            return httpx.Response(
                200, json={"results": [{"id": 1, "value": "a"}], "next_cursor": "A"}
            )
        assert (tmp_path / "mutated").exists()
        return httpx.Response(
            200,
            json={
                "results": [{"id": 2 if snapshot else 3, "value": "b"}],
                "next_cursor": None,
            },
        )

    report = run(
        config(
            consistency="snapshot",
            response={"snapshot_fields": ["$"]},
            oracle={"url": "https://api.test/oracle", "items": "$", "id": "$.id"},
            mutations=[
                {
                    "after_page": 1,
                    "command": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('mutated').touch()",
                    ],
                }
            ],
        ),
        cwd=tmp_path,
        transport=httpx.MockTransport(handle),
    )
    assert report.exit_code == expected_exit
    if not snapshot:
        assert {finding.code for finding in report.findings} == {"CP003", "CP010"}


def test_snapshot_detects_content_updates_with_same_identity() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oracle":
            return httpx.Response(200, json=[{"id": 1, "status": "open"}])
        return httpx.Response(
            200,
            json={"results": [{"id": 1, "status": "closed"}], "next_cursor": None},
        )

    report = run(
        config(
            consistency="snapshot",
            response={"snapshot_fields": ["$"]},
            oracle={"url": "https://api.test/oracle", "items": "$", "id": "$.id"},
        ),
        transport=httpx.MockTransport(handle),
    )
    assert report.exit_code == 1
    assert "CP011" in {finding.code for finding in report.findings}
