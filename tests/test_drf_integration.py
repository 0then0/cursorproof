"""Opt-in real framework regression; synthetic tests need no Django installation."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    importlib.util.find_spec("django") is None
    or importlib.util.find_spec("rest_framework") is None,
    reason="Requires the locked integration dependency group",
)
def test_real_drf_http_orm_regression_and_replay(tmp_path: Path) -> None:
    harness = Path(__file__).resolve().parents[1] / "examples" / "drf_regression.py"
    result = subprocess.run(
        [sys.executable, str(harness), "--output", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["offset_cutoff"] == 1000
    assert len(summary["cases"]) == 9
    for case in summary["cases"]:
        assert case["offline_replay_matches"] and case["live_replay_matches"]
        if case["name"].startswith("problematic"):
            assert case["findings"] and not case["complete"]
        else:
            assert case["complete"] and case["oracle_matches"] and not case["findings"]


@pytest.mark.skipif(
    importlib.util.find_spec("django") is None
    or importlib.util.find_spec("rest_framework") is None,
    reason="Requires the locked integration dependency group",
)
def test_real_fixture_cleanup_after_failed_check() -> None:
    examples = Path(__file__).resolve().parents[1] / "examples"
    program = """
import socket
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen
from django.conf import settings
import drf_regression

try:
    with drf_regression.fixture_server() as (origin, expected, cutoff):
        database = Path(settings.DATABASES['default']['NAME'])
        assert database.exists()
        with urlopen(origin + '/unique/?page_size=500', timeout=2) as response:
            assert response.status == 200
        port = urlsplit(origin).port
        raise RuntimeError('simulated failed regression assertion')
except RuntimeError as error:
    assert str(error) == 'simulated failed regression assertion'
else:
    raise AssertionError('fixture swallowed failure')
assert not database.exists()
try:
    with socket.create_connection(('127.0.0.1', port), timeout=1):
        raise AssertionError('fixture server still accepts connections')
except ConnectionRefusedError:
    pass
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=examples,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
