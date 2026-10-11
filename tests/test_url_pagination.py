"""Synthetic HTTP fixtures; the real Django/ORM run is examples/drf_regression.py."""

import json
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from cursorproof.checks import analyze
from cursorproof.cli import app, save_trace
from cursorproof.config import Config
from cursorproof.models import parse_trace
from cursorproof.privacy import replay_fingerprint
from cursorproof.runner import run


def config(**changes: object) -> Config:
    return Config.model_validate(
        {
            "url": "https://api.test/orders/",
            "limits": [2],
            "pagination": {"mode": "url"},
            "response": {"next_cursor": "$.next"},
            **changes,
        }
    )


def page(ids: list[int], link: object) -> dict[str, object]:
    return {"results": [{"id": item} for item in ids], "next": link}


@pytest.mark.parametrize(
    "link",
    [
        "https://api.test/orders/?limit=2&cursor=bz01MDA%3D",
        "https://API.TEST:443/orders/?limit=2&cursor=bz01MDA%3D",
        "//api.test/orders/?limit=2&cursor=bz01MDA%3D",
        "/orders/?limit=2&cursor=bz01MDA%3D",
        "?limit=2&cursor=bz01MDA%3D",
    ],
)
def test_supported_links_followed_directly_and_replay(link: str, tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json=page([], link))
        assert request.url.query == b"limit=2&cursor=bz01MDA%3D"
        return httpx.Response(200, json=page([1], None))

    report = run(config(), transport=httpx.MockTransport(handle))
    assert report.exit_code == 0
    assert len(requests) == 2
    assert report.trace.traversals[0].stop == "terminal"
    pages = report.trace.traversals[0].pages
    assert pages[1].cursor == pages[0].next_cursor
    assert pages[1].cursor in pages[1].request
    assert "bz01MDA" not in report.model_dump_json()
    assert analyze(parse_trace(report.trace.model_dump_json().encode())) == report
    trace_path = tmp_path / "trace.json"
    save_trace(report.trace, trace_path)
    replay = CliRunner().invoke(app, ["replay", str(trace_path), "--format", "json"])
    assert replay.exit_code == 0
    assert json.loads(replay.stdout) == report.model_dump(mode="json")


def test_path_relative_link_resolves_against_current_request() -> None:
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            200,
            json=page([len(requests)], "orders?limit=2&cursor=a" if len(requests) == 1 else None),
        )

    report = run(config(url="https://api.test/v1/orders"), transport=httpx.MockTransport(handle))
    assert report.exit_code == 0
    assert requests[1] == "https://api.test/v1/orders?limit=2&cursor=a"


def test_query_only_link_uses_current_continuation_query() -> None:
    # Relative references replace the current query rather than merge the initial query.
    responses = iter([page([], "?limit=2&cursor=A"), page([], "orders/?limit=2&cursor=B")])
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json=next(responses))

    # The path-relative reference resolves to /orders/orders/, which is forbidden.
    report = run(config(), transport=httpx.MockTransport(handle))
    assert report.exit_code == 2
    assert len(requests) == 2
    assert "endpoint path" in report.errors[0].message


@pytest.mark.parametrize("link", [0, False, [], {}, "", " "])
def test_invalid_type_or_empty_link_is_execution_error(link: object) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=page([], link))

    report = run(config(), transport=httpx.MockTransport(handle))
    assert report.exit_code == 2
    assert len(requests) == 1
    assert report.trace.traversals[0].stop == "error"
    assert not report.findings
    assert analyze(report.trace) == report


def test_missing_continuation_field_is_explicit_error() -> None:
    report = run(
        config(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": []})),
    )
    assert report.exit_code == 2
    assert "continuation field is missing" in report.errors[0].message


@pytest.mark.parametrize(
    "link",
    [
        "https://other.test/orders/?limit=2&cursor=private",
        "http://api.test/orders/?limit=2&cursor=private",
        "https://api.test:8443/orders/?limit=2&cursor=private",
        "https://api.test/other/?limit=2&cursor=private",
        "https://api.test/orders?limit=2&cursor=private",
        "https://api.test/%6Frders/?limit=2&cursor=private",
        "https://user:password@api.test/orders/?limit=2",
        "//user@api.test/orders/?limit=2",
        "ftp://api.test/orders/?limit=2",
        "file:///orders/?limit=2",
        "javascript:alert(1)",
        "http:orders/?limit=2",
        "?limit=2&cursor=private#fragment",
        "?limit=2#",
        "?limit=2&cursor=%XX",
        "?limit=2&cursor=bad value",
        "?limit=2&cursor=bad\nvalue",
        "https://[invalid/orders/?limit=2",
        "https://api.test:bad/orders/?limit=2",
        "https://api.test\\@other.test/orders/?limit=2",
        "/orders/../orders/?limit=2",
        "/orders/%2e%2e/orders/?limit=2",
        "/orders%2f?limit=2",
        "/orders%5c?limit=2",
        "?limit=2&cursor=nonascii-é",
        '?limit=2&cursor=a"b',
        "?limit=2&cursor=<a>",
        "?limit=2&cursor=a^b",
        "?limit=2&cursor=a`b",
        "?limit=2&cursor={a}",
        "?limit=2&cursor=a|b",
    ],
)
def test_forbidden_target_never_receives_request_or_credentials(link: str) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert len(requests) == 1, "Forbidden continuation received an HTTP request"
        assert request.headers["authorization"] == "Bearer private"
        return httpx.Response(200, json=page([1], link))

    report = run(
        config(headers={"Authorization": "Bearer private"}), transport=httpx.MockTransport(handle)
    )
    assert report.exit_code == 2
    assert len(requests) == 1
    assert not report.findings
    saved = report.model_dump_json()
    assert "password" not in saved
    assert "private" not in saved
    assert "other.test" not in saved


@pytest.mark.parametrize(
    "query",
    [
        "limit=3&tenant=a&tag=x&tag=y&cursor=A",
        "tenant=a&tag=x&tag=y&cursor=A",
        "limit=2&limit=2&tenant=a&tag=x&tag=y&cursor=A",
        "limit=02&tenant=a&tag=x&tag=y&cursor=A",
        "limit=2&tenant=b&tag=x&tag=y&cursor=A",
        "limit=2&tag=x&tag=y&cursor=A",
        "limit=2&tenant=a&tag=x&cursor=A",
        "limit=2&tenant=a&tag=y&tag=x&cursor=A",
        "limit=2&tenant=a&tag=x&tag=y&extra=1&cursor=A",
        "limit=2&tenant=a&tenant=a&tag=x&tag=y&cursor=A",
    ],
)
def test_scope_and_page_size_changes_rejected(query: str) -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert len(requests) == 1
        return httpx.Response(200, json=page([], f"?{query}"))

    report = run(
        config(url="https://api.test/orders/?tenant=a&tag=x&tag=y"),
        transport=httpx.MockTransport(handle),
    )
    assert report.exit_code == 2
    assert "scope parameters and page size" in report.errors[0].message


def test_scope_override_repeated_parameters_and_opaque_query_preserved() -> None:
    query = b"cursor=A%2fb+B%20C%3d&tag=x&limit=2&tag=y&tenant=chosen&empty="
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            assert parse_qsl(request.url.query.decode(), keep_blank_values=True) == [
                ("tag", "x"),
                ("tag", "y"),
                ("tenant", "chosen"),
                ("empty", ""),
                ("limit", "2"),
            ]
            return httpx.Response(200, json=page([1], "?" + query.decode()))
        assert request.url.query == query
        return httpx.Response(200, json=page([2], None))

    report = run(
        config(
            url="https://api.test/orders/?tag=x&tag=y&tenant=old&empty=",
            parameters={"tenant": "chosen"},
        ),
        transport=httpx.MockTransport(handle),
    )
    assert report.exit_code == 0
    assert len(requests) == 2


def test_continuation_can_preserve_repeated_cursor_values_without_rebuilding() -> None:
    queries: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.query)
        return httpx.Response(
            200, json=page([], "?limit=2&cursor=A&cursor=B" if len(queries) == 1 else None)
        )

    assert run(config(), transport=httpx.MockTransport(handle)).exit_code == 0
    assert queries[1] == b"limit=2&cursor=A&cursor=B"


@pytest.mark.parametrize(
    "query",
    [
        b"limit=2&cursor=%FF",
        b"cursor=%FE%00%ff&limit=2",
        b"limit=2&cursor=%FF&cursor=%FE",
        b"limit=2&%63ursor=%FF&cursor=+%20%2f",
    ],
)
def test_binary_cursor_query_preserved_with_redaction_and_replay(query: bytes) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json=page([1], "?" + query.decode("ascii")))
        assert request.url.query == query
        return httpx.Response(200, json=page([2], None))

    report = run(config(), transport=httpx.MockTransport(handle))
    assert report.exit_code == 0
    assert len(requests) == 2
    assert report.trace.traversals[0].stop == "terminal"
    pages = report.trace.traversals[0].pages
    assert pages[1].cursor == pages[0].next_cursor
    assert pages[1].cursor in pages[1].request
    for raw in ("%FF", "%FE", "%ff", "%00", "%2f"):
        assert raw not in report.model_dump_json()
    assert analyze(parse_trace(report.trace.model_dump_json().encode())) == report


def test_binary_cursor_uses_configured_parameter_name() -> None:
    queries: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.query)
        return httpx.Response(
            200, json=page([len(queries)], "?limit=2&%73tate=%FE" if len(queries) == 1 else None)
        )

    report = run(
        config(pagination={"mode": "url", "cursor_param": "state"}),
        transport=httpx.MockTransport(handle),
    )
    assert report.exit_code == 0
    assert len(queries) == 2
    assert queries[1] == b"limit=2&%73tate=%FE"


@pytest.mark.parametrize("scope", ["tenant=%FF", "tenant=a&tenant=%FE", "%FF=a"])
def test_binary_cursor_does_not_allow_invalid_utf8_scope(scope: str) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=page([], f"?{scope}&limit=2&cursor=%FF"))

    report = run(
        config(url="https://api.test/orders/?tenant=a"), transport=httpx.MockTransport(handle)
    )
    assert report.exit_code == 2
    assert len(requests) == 1
    assert report.trace.traversals[0].stop == "error"
    assert "valid UTF-8" in report.errors[0].message


def test_short_and_empty_pages_with_links_do_not_terminate() -> None:
    responses = iter(
        [page([1], "?limit=2&cursor=A"), page([], "?limit=2&cursor=B"), page([2], None)]
    )
    report = run(
        config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=next(responses)))
    )
    assert report.exit_code == 0
    assert report.summary.pages == 3


@pytest.mark.parametrize("ids", [[], [1]])
def test_url_cycle_stops_without_completeness_claim(ids: list[int]) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oracle":
            return httpx.Response(200, json=[1, 2, 3])
        return httpx.Response(200, json=page(ids, "?limit=2&cursor=A"))

    report = run(
        config(oracle={"url": "https://api.test/oracle"}), transport=httpx.MockTransport(handle)
    )
    assert report.exit_code == 1  # Existing contract: witnessed cycles are invariant violations.
    assert report.summary.pages == 2
    assert report.trace.traversals[0].stop == "cycle"
    assert "CP001" in {finding.code for finding in report.findings}
    assert "CP003" not in {finding.code for finding in report.findings}
    assert analyze(parse_trace(report.trace.model_dump_json().encode())) == report


def test_different_text_forms_are_not_evidence_of_item_progress() -> None:
    responses = iter(
        [
            page([1], "?limit=2&cursor=A%2f"),
            page([1], "?cursor=A%2F&limit=2"),
            page([1], "?cursor=A%2f&limit=2"),
        ]
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oracle":
            return httpx.Response(200, json=[1, 2])
        return httpx.Response(200, json=next(responses))

    report = run(
        config(max_pages=3, oracle={"url": "https://api.test/oracle"}),
        transport=httpx.MockTransport(handle),
    )
    assert report.exit_code == 2
    assert report.trace.traversals[0].stop == "budget"
    assert {finding.code for finding in report.findings} == {"CP002"}
    aliases = [p.next_cursor for p in report.trace.traversals[0].pages]
    assert len(set(aliases)) == 3  # Equality does not rewrite the opaque query.
    assert analyze(report.trace) == report


def test_repeated_url_with_new_items_is_followed() -> None:
    responses = iter(
        [page([1], "?limit=2&cursor=A"), page([2], "?limit=2&cursor=A"), page([3], None)]
    )
    report = run(
        config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=next(responses)))
    )
    assert report.exit_code == 0
    assert report.summary.pages == 3


def test_continuation_url_sensitive_query_and_header_redaction() -> None:
    queries: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.query)
        return httpx.Response(
            200,
            json=page(
                [], "?limit=2&api_key=superprivate&cursor=opaque%3D" if len(queries) == 1 else None
            ),
        )

    report = run(
        config(
            url="https://api.test/orders/?api_key=superprivate", headers={"X-Secret": "credential"}
        ),
        transport=httpx.MockTransport(handle),
    )
    assert report.exit_code == 0
    assert b"superprivate" in queries[1]
    saved = report.model_dump_json()
    for raw in ["superprivate", "opaque", "credential"]:
        assert raw not in saved
    assert "REDACTED" in saved
    assert analyze(parse_trace(report.trace.model_dump_json().encode())) == report


@pytest.mark.parametrize("status", [301, 302, 307, 401, 500])
def test_url_mode_does_not_follow_redirects(status: int) -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://other.test/orders/"})

    report = run(config(), transport=httpx.MockTransport(handle))
    assert report.exit_code == 2
    assert len(requests) == 1
    assert not report.findings


@pytest.mark.parametrize(
    "changes",
    [
        {"pagination": {"mode": "automatic"}},
        {"pagination": {"mode": "url", "terminal_values": [None, ""]}},
        {"pagination": {"mode": "url", "terminal_values": [""]}},
        {"cursor_binding": {"parameters": {"tenant": ["b"]}}, "parameters": {"tenant": "a"}},
        {"boundary_testing": {"setup": ["fixture", "{count}"], "cleanup": ["fixture", "reset"]}},
        {"url": "https://api.test/orders/../orders/"},
    ],
)
def test_url_configuration_conflicts_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        config(**changes)


def test_token_mode_url_like_token_and_historical_fingerprint_unchanged() -> None:
    token = "https://other.test/path?state=opaque"
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"results": [], "next_cursor": token})
        assert request.url.host == "api.test"
        assert request.url.params["cursor"] == token
        return httpx.Response(200, json={"results": [], "next_cursor": None})

    legacy = Config(url="https://api.test/orders", limits=[2])
    explicit = Config(url="https://api.test/orders", limits=[2], pagination={"mode": "token"})
    assert legacy.pagination.mode == "token"
    assert (
        replay_fingerprint(legacy)
        == "74210238b8ec4402907c12fec4a6488b4146bd5c3913c27cfe02e5dc9c83f075"
    )
    assert replay_fingerprint(legacy) == replay_fingerprint(explicit)
    assert replay_fingerprint(legacy) != replay_fingerprint(
        legacy.model_copy(update={"pagination": config().pagination})
    )
    assert run(legacy, transport=httpx.MockTransport(handle)).exit_code == 0
    assert len(requests) == 2


def test_url_live_replay_validates_mode_and_reissues_links(tmp_path: Path, monkeypatch) -> None:
    configs = tmp_path / "config.json"
    trace = tmp_path / "trace.json"
    configs.write_text(config().model_dump_json())
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json=page([], None if "cursor" in request.url.params else "?limit=2&cursor=A")
        )

    monkeypatch.setattr(
        "cursorproof.cli.execute", lambda cfg, **kw: run(cfg, transport=httpx.MockTransport(handle))
    )
    runner = CliRunner()
    result = runner.invoke(app, ["run", str(configs), "--repro", str(trace), "--format", "json"])
    assert result.exit_code == 0
    result = runner.invoke(
        app, ["replay", str(trace), "--config", str(configs), "--format", "json"]
    )
    assert result.exit_code == 0
    assert len(calls) == 4
    changed = config(pagination={"mode": "token"})
    configs.write_text(changed.model_dump_json())
    result = runner.invoke(
        app, ["replay", str(trace), "--config", str(configs), "--format", "json"]
    )
    assert result.exit_code == 2
    assert "contract does not match" in result.stdout
    assert len(calls) == 4


def test_configured_nested_continuation_and_has_more_checks() -> None:
    report = run(
        config(response={"next_cursor": "$.links.next", "has_more": "$.has_more"}),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"results": [{"id": 1}], "links": {"next": None}, "has_more": True}
            )
        ),
    )
    assert report.exit_code == 1
    assert {finding.code for finding in report.findings} == {"CP008"}


def test_url_multi_page_cycle_reproduces_existing_checks() -> None:
    responses = iter(
        [
            page([1], "?limit=2&cursor=A"),
            page([2], "?limit=2&cursor=B"),
            page([3], "?limit=2&cursor=A"),
            page([2], "?limit=2&cursor=B"),
        ]
    )
    report = run(
        config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=next(responses)))
    )
    assert report.exit_code == 1
    assert report.trace.traversals[0].stop == "cycle"
    assert {finding.code for finding in report.findings} == {"CP002", "CP006"}
    assert analyze(report.trace) == report


@pytest.mark.parametrize("budget", [{"max_items": 1}, {"max_response_bytes": 1}])
def test_url_item_and_response_budgets_are_incomplete(budget: dict[str, int]) -> None:
    report = run(
        config(**budget),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=page([1, 2], None))),
    )
    assert report.exit_code == 2
    assert report.trace.traversals[0].stop in {"budget", "error"}
    assert not report.findings
    assert analyze(report.trace) == report


def test_url_scope_invalid_utf8_is_not_equivalent_replacement_text() -> None:
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=page([], "?tenant=%FE&limit=2&cursor=A"))

    report = run(
        config(url="https://api.test/orders/?tenant=%FF"), transport=httpx.MockTransport(handle)
    )
    assert report.exit_code == 2
    assert len(requests) == 1
    assert "valid UTF-8" in report.errors[0].message


def test_url_snapshot_full_record_checks_preserved() -> None:
    def handle(request):
        if request.url.path == "/oracle":
            return httpx.Response(200, json=[{"id": 1, "value": "before"}])
        return httpx.Response(200, json={"results": [{"id": 1, "value": "after"}], "next": None})

    report = run(
        config(
            consistency="snapshot",
            response={"next_cursor": "$.next", "snapshot_fields": ["$"]},
            oracle={"url": "https://api.test/oracle", "id": "$.id"},
        ),
        transport=httpx.MockTransport(handle),
    )
    assert report.exit_code == 1
    assert {finding.code for finding in report.findings} == {"CP011"}
    assert analyze(parse_trace(report.trace.model_dump_json().encode())) == report


def test_url_live_keyset_order_and_identity_checks_preserved() -> None:
    responses = iter([page([2], "?limit=2&cursor=A"), page([1, 2], None)])
    report = run(
        config(consistency="live-keyset", immutable_ordering=True, ordering=[{"field": "id"}]),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=next(responses))),
    )
    assert report.exit_code == 1
    assert {finding.code for finding in report.findings} == {"CP002", "CP004"}
    assert "does not establish completeness" in " ".join(report.notes)
    assert analyze(report.trace) == report


@pytest.mark.parametrize(
    ("case", "exit_code", "stop"), [("problematic", 1, "cycle"), ("control", 0, "terminal")]
)
def test_saved_real_http_evidence_replays_without_framework(
    case: str, exit_code: int, stop: str
) -> None:
    trace = Path(__file__).resolve().parents[1] / "docs" / f"drf-{case}-500-trace.json"
    result = CliRunner().invoke(app, ["replay", str(trace), "--format", "json"])
    assert result.exit_code == exit_code
    report = json.loads(result.stdout)
    assert report["trace"]["traversals"][0]["stop"] == stop
    assert report["summary"]["unique_items"] == (1500 if case == "problematic" else 2500)
    assert not report["errors"]
    assert "CP003" not in {finding["code"] for finding in report["findings"]}
