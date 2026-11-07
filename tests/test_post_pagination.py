"""POST pagination with a JSON body, string and integer cursors."""

import copy
import json
import runpy
import sys
from collections.abc import Iterator
from datetime import date
from http.server import HTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from cursorproof.checks import analyze, cursor_page_state
from cursorproof.cli import app
from cursorproof.config import Config
from cursorproof.models import parse_trace
from cursorproof.privacy import Redactor, has_http_credentials, replay_fingerprint
from cursorproof.runner import run

EXAMPLES = Path(__file__).parents[1] / "examples"
FILTER = {"must": [{"key": "group", "match": {"value": "keep"}}]}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    module = runpy.run_path(str(EXAMPLES / "scroll_api.py"))
    server = HTTPServer(("127.0.0.1", 0), module["Handler"])
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            base=f"http://127.0.0.1:{server.server_port}",
            handler=module["Handler"],
            fixed=module["FIXED"],
            expected=module["expected_ids"],
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def scroll(base: str, dataset: str, **changes: Any) -> Config:
    ordering_type = "string" if dataset == "uuid" else "number"
    return Config.model_validate(
        {
            "url": f"{base}/scroll/{dataset}",
            "method": "POST",
            "body": {"filter": FILTER, "with_payload": ["group"], "with_vector": False},
            "pagination": {"cursor_param": "offset", "limit_param": "limit"},
            "response": {
                "items": "$.result.points",
                "next_cursor": "$.result.next_page_offset",
                "id": "$.id",
            },
            # Digit strings sort lexicographically, unlike the numeric stream order.
            "ordering": [] if dataset == "digits" else [{"field": "id", "type": ordering_type}],
            "limits": [1, 3, 4, 20],
            "oracle": {"url": f"{base}/oracle/{dataset}"},
            **changes,
        }
    )


def post_config(**changes: Any) -> Config:
    return Config.model_validate(
        {
            "url": "https://api.test/scroll",
            "method": "POST",
            "body": {"filter": FILTER},
            "pagination": {"cursor_param": "offset"},
            "response": {
                "items": "$.result.points",
                "next_cursor": "$.result.next_page_offset",
                "id": "$.id",
            },
            "limits": [2],
            **changes,
        }
    )


def body_page(ids: list[int | str], cursor: object = None) -> dict[str, Any]:
    return {
        "result": {"points": [{"id": item} for item in ids], "next_page_offset": cursor},
    }


def scripted(pages: list[dict[str, Any]], seen: list[httpx.Request] | None = None):
    responses = iter(pages)

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=next(responses))

    return httpx.MockTransport(handle)


def codes(report: Any) -> set[str]:
    return {finding.code for finding in report.findings}


# --- configuration ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"method": "GET"}, "body requires method: POST"),
        (
            {"pagination": {"mode": "url", "cursor_param": "offset"}},
            "requires pagination.mode: token",
        ),
        ({"body": {"offset": 1}}, "not the JSON body"),
        ({"body": {"limit": 5}}, "not the JSON body"),
        ({"body": {"when": date(2024, 1, 1)}}, "not a JSON value"),
        ({"body": {"x": float("inf")}}, "finite JSON number"),
        ({"body": {"x": {1: "a"}}}, "non-string object key"),
        ({"body": {"x": [1, {2, 3}]}}, "not a JSON value"),
        ({"url": "https://api.test/scroll?offset=1"}, "not URL or parameters"),
        ({"parameters": {"limit": 5}}, "not URL or parameters"),
        ({"pagination": {"terminal_values": [True]}}, "terminal_values"),
        ({"pagination": {"terminal_values": [1.5]}}, "terminal_values"),
    ],
)
def test_invalid_post_contracts_are_rejected(changes: dict[str, Any], reason: str) -> None:
    with pytest.raises(ValidationError) as caught:
        post_config(**changes)
    assert reason in str(caught.value)


def test_excessively_nested_body_is_a_configuration_error() -> None:
    def nested(depth: int) -> Any:
        value: Any = "leaf"
        for _ in range(depth):
            value = [value]
        return value

    assert post_config(body={"a": nested(60)})
    for depth in (70, 900):
        with pytest.raises(ValidationError, match="nested deeper than 64 levels"):
            post_config(body={"a": nested(depth)})


def test_get_configuration_rejects_body_and_integer_terminals() -> None:
    with pytest.raises(ValidationError, match="body requires method: POST"):
        Config.model_validate({"url": "https://api.test/x", "body": {}})
    with pytest.raises(ValidationError, match="Integer terminal_values require method: POST"):
        Config.model_validate(
            {"url": "https://api.test/x", "pagination": {"terminal_values": [None, 0]}}
        )
    assert post_config(pagination={"cursor_param": "offset", "terminal_values": [None, 0]})


def test_binding_on_body_field_is_rejected_with_reason() -> None:
    with pytest.raises(ValidationError, match="query parameters only"):
        post_config(cursor_binding={"parameters": {"filter": ["x"]}})


def test_post_without_body_sends_only_pagination_fields() -> None:
    seen: list[httpx.Request] = []
    config = post_config(body=None)
    report = run(config, transport=scripted([body_page([1], 7), body_page([2])], seen))
    assert report.exit_code == 0
    assert [json.loads(request.content) for request in seen] == [
        {"limit": 2},
        {"limit": 2, "offset": 7},
    ]


# --- traversal against the strict local API --------------------------------------------------


@pytest.mark.parametrize("dataset", ["int", "zero", "big", "uuid", "digits"])
def test_each_cursor_kind_traverses_all_page_sizes(api: SimpleNamespace, dataset: str) -> None:
    config = scroll(api.base, dataset)
    before = copy.deepcopy(config.model_dump())
    report = run(config)
    assert report.exit_code == 0, report.errors or report.findings
    assert api.handler.violations == []
    assert config.model_dump() == before
    assert report.trace.request_method == "POST"
    assert report.summary.unique_items == len(api.expected(dataset))
    # Every request carries the same fixed fields; only limit and offset vary.
    for body in api.handler.requests:
        assert {k: v for k, v in body.items() if k not in {"limit", "offset"}} == api.fixed
    assert {body["limit"] for body in api.handler.requests} == {1, 3, 4, 20}
    assert analyze(report.trace) == report


def test_stream_matches_the_independent_expected_ids(api: SimpleNamespace) -> None:
    for dataset in ("int", "zero", "big", "uuid", "digits"):
        report = run(scroll(api.base, dataset, limits=[4]))
        streams = [
            [item.id for page in t.pages for item in page.items] for t in report.trace.traversals
        ]
        assert streams == [api.expected(dataset)]


def test_zero_cursor_is_sent_as_integer_zero(api: SimpleNamespace) -> None:
    report = run(scroll(api.base, "zero", limits=[5]))
    assert report.exit_code == 0
    offsets = [body.get("offset", "<absent>") for body in api.handler.requests]
    assert offsets[0] == "<absent>"
    assert offsets[1] == 0
    assert type(offsets[1]) is int
    assert report.trace.traversals[0].pages[0].next_cursor == "cursor_1"


def test_large_integers_keep_their_exact_value(api: SimpleNamespace) -> None:
    report = run(scroll(api.base, "big", limits=[3]))
    assert report.exit_code == 0
    offsets = [body["offset"] for body in api.handler.requests if "offset" in body]
    assert all(type(offset) is int for offset in offsets)
    assert 2**53 + 1 in {item.id for p in report.trace.traversals[0].pages for item in p.items}
    assert 2**64 - 1 in {item.id for p in report.trace.traversals[0].pages for item in p.items}
    assert any(offset > 2**53 for offset in offsets)


def test_digit_strings_are_not_turned_into_numbers(api: SimpleNamespace) -> None:
    report = run(scroll(api.base, "digits", limits=[4]))
    assert report.exit_code == 0
    assert api.handler.violations == []
    assert all(type(body["offset"]) is str for body in api.handler.requests if "offset" in body)


@pytest.mark.parametrize(
    ("fault", "expected"),
    [("dup", {"CP002"}), ("skip", {"CP003"}), ("cycle", {"CP006"})],
)
def test_controlled_faults_are_found_by_run_and_offline_replay(
    api: SimpleNamespace, tmp_path: Path, fault: str, expected: set[str]
) -> None:
    config = scroll(api.base, fault, limits=[3], ordering=[{"field": "id", "type": "number"}])
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json())
    trace_path = tmp_path / "trace.json"
    result = CliRunner().invoke(
        app, ["run", str(config_path), "--format", "json", "--repro", str(trace_path)]
    )
    assert result.exit_code == 1, result.output
    live = json.loads(result.stdout)
    assert expected <= {finding["code"] for finding in live["findings"]}
    assert api.handler.violations == []
    replayed = CliRunner().invoke(app, ["replay", str(trace_path), "--format", "json"])
    assert replayed.exit_code == 1
    assert json.loads(replayed.stdout)["findings"] == live["findings"]


# --- cursor typing ---------------------------------------------------------------------------


def test_cursor_types_survive_into_the_next_request() -> None:
    seen: list[httpx.Request] = []
    report = run(
        post_config(),
        transport=scripted(
            [body_page([1], 0), body_page([2], "0"), body_page([3], 10), body_page([4])], seen
        ),
    )
    assert report.exit_code == 0
    bodies = [json.loads(request.content) for request in seen]
    assert [body.get("offset", "<none>") for body in bodies] == ["<none>", 0, "0", 10]
    assert [type(body.get("offset")).__name__ for body in bodies[1:]] == ["int", "str", "int"]
    pages = report.trace.traversals[0].pages
    assert len({page.next_cursor for page in pages[:-1]}) == 3


def test_number_and_string_cursors_are_different_page_states() -> None:
    assert cursor_page_state(1, [5]) != cursor_page_state("1", [5])
    assert cursor_page_state(0, []) != cursor_page_state("0", [])
    assert cursor_page_state(True, []) != cursor_page_state(1, [])  # type: ignore[arg-type]
    redactor = Redactor(set())
    assert redactor.cursor(1) != redactor.cursor("1")
    assert redactor.cursor(1) == redactor.cursor(1)


def test_number_then_identical_string_is_not_a_false_cycle() -> None:
    report = run(
        post_config(),
        transport=scripted([body_page([1], 1), body_page([1], "1"), body_page([2])]),
    )
    # The repeated ID is a duplicate, but the cursor change is not reported as a loop.
    assert codes(report) == {"CP002"}
    assert report.summary.pages == 3
    assert analyze(report.trace) == report


def test_same_integer_cursor_with_same_page_is_a_cycle() -> None:
    report = run(post_config(), transport=scripted([body_page([1], 5), body_page([1], 5)] * 2))
    assert "CP001" in codes(report)
    assert report.trace.traversals[0].stop == "cycle"
    assert analyze(report.trace) == report


@pytest.mark.parametrize("cursor", [True, False, 1.0, 1.5, [1], {"id": 1}])
def test_invalid_cursor_types_are_errors(cursor: object) -> None:
    report = run(post_config(), transport=scripted([body_page([1], cursor)]))
    assert report.exit_code == 2
    assert report.errors[0].message == "Cursor must be a string, an integer or null"


def test_integer_cursor_is_still_rejected_for_get_requests() -> None:
    config = Config.model_validate({"url": "https://api.test/orders", "limits": [2]})
    report = run(
        config,
        transport=scripted([{"results": [{"id": 1}], "next_cursor": 5}]),
    )
    assert report.exit_code == 2
    assert report.errors[0].message == "Cursor must be a string or null"


def test_terminal_values_compare_type_and_value() -> None:
    config = post_config(pagination={"cursor_param": "offset", "terminal_values": [None, 0, "end"]})
    report = run(config, transport=scripted([body_page([1], 7), body_page([2], 0)]))
    assert report.exit_code == 0
    assert report.trace.traversals[0].stop == "terminal"
    assert report.trace.traversals[0].pages[-1].next_cursor is None

    config = post_config(pagination={"cursor_param": "offset", "terminal_values": [None, "0"]})
    seen: list[httpx.Request] = []
    report = run(config, transport=scripted([body_page([1], 0), body_page([2])], seen))
    assert report.exit_code == 0
    assert json.loads(seen[1].content)["offset"] == 0


def test_requests_do_not_follow_redirects_or_change_the_method() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(307, headers={"Location": "https://other.test/scroll"})

    report = run(post_config(), transport=httpx.MockTransport(handle))
    assert report.exit_code == 2
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert "HTTP 307" in report.errors[0].message


# --- body fields stay fixed ------------------------------------------------------------------


def test_only_limit_and_cursor_change_and_query_parameters_are_kept() -> None:
    seen: list[httpx.Request] = []
    body = {"filter": FILTER, "nested": {"list": [1, 2, {"a": None}]}, "flag": True}
    config = post_config(
        url="https://api.test/scroll?consistency=majority",
        parameters={"timeout": 5},
        body=body,
        limits=[2, 3],
    )
    original = copy.deepcopy(config.body)
    report = run(config, transport=scripted([body_page([1], 9), body_page([2])] * 2, seen))
    assert report.exit_code == 0
    assert config.body == original
    assert {request.method for request in seen} == {"POST"}
    assert {request.url.query for request in seen} == {b"consistency=majority&timeout=5"}
    for request in seen:
        sent = json.loads(request.content)
        assert {k: v for k, v in sent.items() if k not in {"limit", "offset"}} == body
        assert request.headers["content-type"] == "application/json"
    assert [json.loads(r.content)["limit"] for r in seen] == [2, 2, 3, 3]


# --- trace, replay and privacy ---------------------------------------------------------------


def test_trace_records_method_and_body_shape_without_values() -> None:
    config = post_config(
        body={
            "filter": {"must": [{"key": "tenant", "match": {"value": "acme-42"}}]},
            "api_key": "k-9",
        }
    )
    report = run(config, transport=scripted([body_page([1], 3), body_page([2])]))
    trace = report.trace
    assert trace.request_method == "POST"
    assert trace.request_body is not None
    assert trace.request_body.fixed_fields == ["api_key", "filter"]
    assert (trace.request_body.cursor_field, trace.request_body.limit_field) == ("offset", "limit")
    text = trace.model_dump_json()
    assert "acme-42" not in text
    assert "k-9" not in text
    assert "tenant" not in text


def test_environment_values_in_body_are_not_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TENANT_TOKEN", "s3cr3t-tenant")
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "url": "https://api.test/scroll?token=s3cr3t-tenant",
                "method": "POST",
                "body": {"filter": {"tenant": "${TENANT_TOKEN}"}},
                "pagination": {"cursor_param": "offset"},
                "response": {
                    "items": "$.result.points",
                    "next_cursor": "$.result.next_page_offset",
                    "id": "$.id",
                },
                "limits": [2],
            }
        )
    )
    from cursorproof.config import load_config

    config, secrets = load_config(path)
    report = run(config, secrets=secrets, transport=scripted([body_page(["s3cr3t-tenant"])]))
    assert "s3cr3t-tenant" not in report.model_dump_json()


def test_fingerprint_detects_method_and_body_changes_but_not_credential_rotation() -> None:
    base = post_config(body={"filter": FILTER, "api_key": "one"})
    original = replay_fingerprint(base)
    assert original is not None
    assert replay_fingerprint(post_config(body={"filter": FILTER, "api_key": "two"})) == original
    assert replay_fingerprint(post_config(body={"filter": FILTER, "token": "x"})) != original
    changed_field = {"must": [{"key": "other", "match": {"value": "keep"}}]}
    changed_value = {"must": [{"key": "group", "match": {"value": "drop"}}]}
    for body in (
        {"filter": changed_field, "api_key": "one"},
        {"filter": changed_value, "api_key": "one"},
        {"filter": FILTER, "api_key": "one", "with_payload": True},
        {"filter": FILTER},
    ):
        assert replay_fingerprint(post_config(body=body)) != original
    # Numbers and strings differ in the contract.
    assert replay_fingerprint(post_config(body={"a": 1})) != replay_fingerprint(
        post_config(body={"a": "1"})
    )
    assert replay_fingerprint(post_config(body={"a": 1})) != replay_fingerprint(
        post_config(body={"a": True})
    )
    assert replay_fingerprint(post_config(pagination={"cursor_param": "start"})) != (
        replay_fingerprint(post_config())
    )
    get = Config.model_validate({"url": "https://api.test/scroll", "limits": [2]})
    assert replay_fingerprint(get) != replay_fingerprint(post_config(body=None))


def test_get_fingerprints_are_unchanged() -> None:
    # Computed with CursorProof 0.2.1 before POST support.
    plain = Config.model_validate({"url": "https://api.test/orders", "limits": [2]})
    assert (
        replay_fingerprint(plain)
        == "74210238b8ec4402907c12fec4a6488b4146bd5c3913c27cfe02e5dc9c83f075"
    )
    rich = Config.model_validate(
        {
            "url": "http://127.0.0.1:8765/correct?status=open",
            "limits": [1, 5],
            "headers": {"Authorization": "Bearer x", "X-Tenant": "a"},
            "parameters": {"tenant": "t1"},
            "pagination": {"terminal_values": [None, ""]},
            "ordering": [{"field": "id", "type": "number"}],
        }
    )
    assert (
        replay_fingerprint(rich)
        == "4b4f6450e119f7938e66f271f700d23aa45e421d2718bf73d0d686668a238ebe"
    )


def test_credential_named_body_field_requires_auth_scope_acknowledgement() -> None:
    assert has_http_credentials(post_config(body={"api_key": "x"}))
    assert not has_http_credentials(post_config(body={"filter": {"key": "group"}}))


def test_historical_get_trace_still_loads_offline(tmp_path: Path) -> None:
    root = Path(__file__).parents[1] / "docs"
    for name in ("drf-problematic-500-trace.json", "drf-control-500-trace.json"):
        trace = parse_trace((root / name).read_bytes())
        assert trace.request_method == "GET"
        assert trace.request_body is None
        analyze(trace)


def test_trace_with_inconsistent_method_evidence_is_incomplete() -> None:
    report = run(post_config(), transport=scripted([body_page([1])]))
    trace = report.trace.model_copy(update={"request_body": None})
    assert analyze(trace).exit_code == 2
    get = run(
        Config.model_validate({"url": "https://api.test/orders", "limits": [2]}),
        transport=scripted([{"results": [], "next_cursor": None}]),
    )
    assert get.trace.request_body is None
    assert analyze(get.trace.model_copy(update={"request_method": "POST"})).exit_code == 2


def test_live_replay_follows_the_saved_contract(api: SimpleNamespace, tmp_path: Path) -> None:
    config = scroll(api.base, "skip", limits=[3])
    path = tmp_path / "config.json"
    path.write_text(config.model_dump_json())
    trace = tmp_path / "trace.json"
    runner = CliRunner()
    assert runner.invoke(app, ["run", str(path), "--repro", str(trace)]).exit_code == 1
    first = api.handler.requests[:]
    api.handler.requests.clear()
    live = runner.invoke(app, ["replay", str(trace), "--config", str(path), "--format", "json"])
    assert live.exit_code == 1, live.output
    assert "CP003" in {finding["code"] for finding in json.loads(live.stdout)["findings"]}
    # Replay rebuilt equivalent POST requests (including integer offsets) from the config.
    assert api.handler.requests == first

    def changed(update: dict[str, Any]) -> int:
        raw = json.loads(path.read_text())
        raw.update(update)
        variant = tmp_path / "variant.json"
        variant.write_text(json.dumps(raw))
        result = runner.invoke(
            app, ["replay", str(trace), "--config", str(variant), "--format", "json"]
        )
        assert result.exit_code == 2
        assert "does not match" in result.stdout
        return result.exit_code

    other_filter = {"must": [{"key": "group", "match": {"value": "drop"}}]}
    assert config.body is not None
    changed({"body": {**config.body, "filter": other_filter}})
    changed({"body": {**config.body, "with_vector": True}})
    changed({"body": {k: v for k, v in config.body.items() if k != "with_payload"}})
    changed({"method": "GET", "body": None})
    changed({"pagination": {"cursor_param": "start", "limit_param": "limit"}})
    changed({"url": config.url + "?consistency=majority"})


# --- compatible checks -----------------------------------------------------------------------


def test_cursor_binding_changes_query_parameters_and_keeps_cursor_in_body() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        if request.url.params["region"] != "eu" and "offset" in body:
            return httpx.Response(409, json={"error": "cursor/query mismatch"})
        return httpx.Response(
            200, json=body_page([1], 5) if "offset" not in body else body_page([2])
        )

    config = post_config(
        parameters={"region": "eu"},
        cursor_binding={"parameters": {"region": ["us"]}},
    )
    report = run(config, transport=httpx.MockTransport(handle))
    assert report.exit_code == 0, report.errors
    probes = [r for r in seen if r.url.params["region"] == "us"]
    assert [("offset" in json.loads(r.content)) for r in probes] == [False, True]
    assert json.loads(probes[1].content)["offset"] == 5
    assert all(r.method == "POST" for r in seen)
    assert [b.phase for b in report.trace.bindings] == ["baseline", "cursor"]


def test_cursor_binding_violation_is_reported_for_post() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json=body_page([2]) if "offset" in body else body_page([1], 5))

    config = post_config(
        parameters={"region": "eu"}, cursor_binding={"parameters": {"region": ["us"]}}
    )
    report = run(config, transport=httpx.MockTransport(handle))
    assert "CP007" in codes(report)


def test_mutation_hooks_and_boundary_testing_run_with_post(tmp_path: Path) -> None:
    marker = tmp_path / "hook"
    config = post_config(
        consistency="live-keyset",
        immutable_ordering=True,
        ordering=[{"field": "id", "type": "number"}],
        mutations=[
            {
                "after_page": 1,
                "command": [
                    sys.executable,
                    "-c",
                    "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text('x')",
                    str(marker),
                ],
            }
        ],
    )
    report = run(config, cwd=tmp_path, transport=scripted([body_page([1], 7), body_page([2])]))
    assert report.exit_code == 0
    assert marker.read_text() == "x"
    assert [m.after_page for m in report.trace.mutations] == [1]

    count_file = tmp_path / "count"
    count_file.write_text("0")

    def handle(request: httpx.Request) -> httpx.Response:
        count = int(count_file.read_text())
        body = json.loads(request.content)
        start = body.get("offset", 0)
        end = min(start + body["limit"], count)
        return httpx.Response(
            200,
            json=body_page(list(range(start, end)), end if end < count else None),
        )

    write = "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])"
    from cursorproof.runner import run_boundaries

    boundary = post_config(
        limits=[3],
        boundary_testing={
            "setup": [sys.executable, "-c", write, str(count_file), "{count}"],
            "cleanup": [sys.executable, "-c", write, str(count_file), "0"],
        },
        ordering=[{"field": "id", "type": "number"}],
    )
    result = run_boundaries(boundary, cwd=tmp_path, transport=httpx.MockTransport(handle))
    assert result.outcome == "pass", [c.error for c in result.cases]


def test_invalid_body_error_does_not_echo_private_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cursorproof.config import ConfigError, load_config

    monkeypatch.setenv("TENANT_TOKEN", "s3cr3t-tenant")
    path = tmp_path / "config.yml"
    path.write_text(
        "url: https://api.test/scroll\n"
        "method: POST\n"
        "body:\n"
        "  filter: {tenant: '${TENANT_TOKEN}', owner: private-owner}\n"
        "  since: 2024-01-01\n"
        "pagination: {cursor_param: offset}\n"
    )
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    message = str(caught.value)
    assert "body.since is not a JSON value" in message
    assert "s3cr3t-tenant" not in message
    assert "private-owner" not in message
    assert "2024" not in message
