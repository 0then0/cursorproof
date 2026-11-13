"""Link header continuation in GET URL mode; the real registry run is labs/registry-tags."""

import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from cursorproof.checks import analyze
from cursorproof.cli import app, save_trace
from cursorproof.config import Config, load_config
from cursorproof.links import MAX_LINK_HEADER_BYTES
from cursorproof.models import parse_trace
from cursorproof.privacy import replay_fingerprint
from cursorproof.runner import run

EXAMPLES = Path(__file__).parents[1] / "examples"
LINK = {"mode": "url", "continuation": "link-header"}


def config(**changes: object) -> Config:
    return Config.model_validate(
        {"url": "https://api.test/orders/", "limits": [2], "pagination": LINK, **changes}
    )


def page(ids: list[int], *links: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"results": [{"id": item} for item in ids]},
        headers=[("Link", link.encode("latin-1")) for link in links],
    )


def serve(*responses: httpx.Response) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """Answer the oracle path, then the given pages in order; extra requests fail the test."""
    requests: list[httpx.Request] = []
    pending = iter(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oracle":
            return httpx.Response(200, json=[1, 2, 3, 4, 5])
        requests.append(request)
        return next(pending)

    return httpx.MockTransport(handle), requests


def test_multiple_pages_with_oracle_trace_and_offline_replay(tmp_path: Path) -> None:
    transport, requests = serve(
        page([1, 2], '</orders/?limit=2&cursor=s3cr3t-A>; rel="next"'),
        page([3, 4], '<https://api.test/orders/?cursor=s3cr3t-B&limit=2>; rel="next"'),
        page([5]),
    )
    report = run(
        config(ordering=[{"field": "id"}], oracle={"url": "https://api.test/oracle"}),
        transport=transport,
    )
    assert report.exit_code == 0
    assert report.summary.unique_items == 5
    assert [request.url.query for request in requests] == [
        b"limit=2",
        b"limit=2&cursor=s3cr3t-A",
        b"cursor=s3cr3t-B&limit=2",
    ]
    trace = report.trace
    assert trace.continuation_source == "link-header"
    pages = trace.traversals[0].pages
    assert trace.traversals[0].stop == "terminal"
    assert [p.link.next_links for p in pages if p.link] == [1, 1, 0]
    assert pages[2].link is not None and pages[2].link.field_lines == 0
    assert pages[1].cursor == pages[0].next_cursor and pages[1].cursor in pages[1].request
    # Neither raw targets nor response headers are stored.
    assert "s3cr3t" not in report.model_dump_json()
    trace_path = tmp_path / "trace.json"
    save_trace(trace, trace_path)
    replay = CliRunner().invoke(app, ["replay", str(trace_path), "--format", "json"])
    assert replay.exit_code == 0
    assert json.loads(replay.stdout) == report.model_dump(mode="json")


def test_oracle_detects_items_missing_from_a_link_traversal() -> None:
    transport, _ = serve(page([1, 2], '<?limit=2&cursor=A>; rel="next"'), page([5]))
    report = run(config(oracle={"url": "https://api.test/oracle"}), transport=transport)
    assert report.exit_code == 1
    assert {finding.code for finding in report.findings} == {"CP003"}


@pytest.mark.parametrize(
    "target",
    [
        "/v1/orders?limit=2&cursor=a",
        "orders?limit=2&cursor=a",
        "?limit=2&cursor=a",
        "//api.test/v1/orders?limit=2&cursor=a",
        "https://API.TEST:443/v1/orders?limit=2&cursor=a",
    ],
)
def test_relative_and_absolute_targets_resolve_against_the_request(target: str) -> None:
    transport, requests = serve(page([1], f"<{target}>; rel=next"), page([2]))
    report = run(config(url="https://api.test/v1/orders"), transport=transport)
    assert report.exit_code == 0
    assert str(requests[1].url) == "https://api.test/v1/orders?limit=2&cursor=a"


def test_has_more_from_the_body_is_checked_against_link_termination() -> None:
    def response(more: bool, *links: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [], "more": more},
            headers=[("Link", link) for link in links],
        )

    settings = config(response={"has_more": "$.more"})
    transport, _ = serve(response(True, '<?limit=2&cursor=A>; rel="next"'), response(False))
    assert run(settings, transport=transport).exit_code == 0
    transport, _ = serve(response(True))
    report = run(settings, transport=transport)
    assert report.exit_code == 1
    assert {finding.code for finding in report.findings} == {"CP008"}


def test_query_only_target_replaces_the_current_query() -> None:
    transport, requests = serve(
        page([1], '<?limit=2&cursor=A>; rel="next"'),
        page([2], '<?cursor=B&limit=2>; rel="next"'),
        page([3]),
    )
    assert run(config(), transport=transport).exit_code == 0
    assert requests[2].url.query == b"cursor=B&limit=2"


@pytest.mark.parametrize(
    "links",
    [
        # Several field lines and unrelated relations.
        ['<?limit=2>; rel="first"', '<?limit=2&cursor=N>; rel="next"', "</help>; rel=help"],
        # Several links in one field line, with optional whitespace and empty elements.
        ['<?limit=2>; rel="prev" ,, <?limit=2&cursor=N> ;rel = "next"  ,</x>;rel=last,'],
        # Commas, semicolons, angle brackets and escaped quotes inside quoted parameters.
        [
            r'</a>; rel="help"; title="a, <b>; rel=\"next\", c", '
            r'<?limit=2&cursor=N>; title="x\\"; rel="next"; title*=UTF-8'
            "'en'%e2%82%ac"
        ],
        # A relation list, compared case-insensitively.
        ['<?limit=2&cursor=N>; rel="prefetch NEXT  https://example.test/rel"'],
        ["<?limit=2&cursor=N>; REL=Next; type=text; hreflang=en; flag"],
        # Only the first rel parameter of a link counts.
        ['<?limit=2&cursor=N>; rel="next"; rel="prev"', '</other>; rel="prev"; rel="next"'],
        # Repeated identical next targets, with different parameters.
        ['<?limit=2&cursor=N>; rel="next"', '<?limit=2&cursor=N>; rel="next"; title="again"'],
        # An anchor on an unrelated link does not affect the continuation.
        ['</a>; rel="item"; anchor="#section"', '<?limit=2&cursor=N>; rel="next"'],
        # Obsolete non-ASCII text in an unrelated quoted parameter.
        ['</a>; rel="help"; title="café", <?limit=2&cursor=N>; rel="next"'],
    ],
)
def test_supported_link_syntax_selects_the_single_next_target(links: list[str]) -> None:
    transport, requests = serve(page([1], *links), page([2]))
    report = run(config(), transport=transport)
    assert report.exit_code == 0, report.errors
    assert len(requests) == 2
    assert requests[1].url.query == b"limit=2&cursor=N"
    assert analyze(parse_trace(report.trace.model_dump_json().encode())) == report


@pytest.mark.parametrize(
    "links",
    [
        [],
        [""],
        ['<?limit=2&cursor=N>; rel="prev"', "</help>; rel=help"],
        ["<?limit=2&cursor=N>"],
        ['<?limit=2&cursor=N>; rel="prev"; rel="next"'],
        ['<?limit=2&cursor=N>; title="rel=next"'],
        ['<?limit=2&cursor=N>; rel="nextpage"'],
        ['<?limit=2&cursor=N>; rev="next"'],
    ],
)
def test_parsed_headers_without_next_end_the_traversal(links: list[str]) -> None:
    transport, requests = serve(page([1], *links))
    report = run(config(), transport=transport)
    assert report.exit_code == 0
    assert len(requests) == 1
    assert report.trace.traversals[0].stop == "terminal"
    evidence = report.trace.traversals[0].pages[0].link
    assert evidence is not None and evidence.next_links == 0
    assert evidence.field_lines == len(links)


@pytest.mark.parametrize(
    ("links", "reason"),
    [
        (['<?limit=2&cursor=A>; rel="next"', '<?limit=2&cursor=B>; rel="next"'], "several"),
        (['<?limit=2&cursor=A>; rel="next", <?cursor=A&limit=2>; rel="next"'], "several"),
        (['<?limit=2&cursor=A>; rel="next"', "</orders/?limit=2&cursor=A>; rel=next"], "several"),
        (['<?limit=2&cursor=A>; rel="next"; anchor="/other"'], "anchor"),
        (['<?limit=2&cursor=A>; anchor=""; rel="next"'], "anchor"),
        (['<?limit=2&cursor=A>; rel="next'], "unterminated quoted"),
        (['<?limit=2&cursor=A; rel="next"'], "unterminated <target>"),
        (['?limit=2&cursor=A; rel="next"'], "must start with"),
        (['<?limit=2&cursor=A> rel="next"'], "expected ';' or ','"),
        (['<?limit=2&cursor=A>; rel="next" <x>; rel="prev"'], "expected ';' or ','"),
        (['<?limit=2&cursor=A>; ; rel="next"'], "parameter name"),
        (['<?limit=2&cursor=A>; rel="next";'], "parameter name"),
        (["<?limit=2&cursor=A>; rel="], "parameter name or token"),
        (['<?limit=2&cursor=A>; rel="next"; type=text/html'], "expected ';' or ','"),
        (['<?limit=2&cursor=A>; rel="next"; title="a\x7fb"'], "quoted string"),
        (['<?limit=2&<cursor=A>; rel="next"'], "link target"),
        (['</help>; rel="help"', '<?limit=2&cursor=A>; rel="next', "</x>; rel=x"], "unterminated"),
        (["<" + "a" * MAX_LINK_HEADER_BYTES + '>; rel="help"'], "exceeds"),
        (['</h>; title="' + "a" * 5000 + '"', '</h>; title="' + "a" * 5000 + '"'], "exceeds"),
        # Syntactically valid links whose target fails the existing URL checks.
        (['<>; rel="next"'], "Continuation URL has unsupported or malformed syntax"),
        (['<?limit=2&cursor=a b>; rel="next"'], "Continuation URL has unsupported"),
        (['<?limit=2&cursor=A#frag>; rel="next"'], "Continuation URL has unsupported"),
    ],
)
def test_unsupported_link_evidence_is_an_error_not_a_terminal_page(
    links: list[str], reason: str
) -> None:
    transport, requests = serve(page([1], *links), page([2]))
    report = run(config(), transport=transport)
    assert report.exit_code == 2
    assert len(requests) == 1
    assert report.trace.traversals[0].stop == "error"
    assert not report.trace.traversals[0].pages
    assert not report.findings
    assert reason in report.errors[0].message
    assert "cursor=A" not in report.model_dump_json()
    assert analyze(report.trace) == report


def test_empty_and_short_pages_with_next_continue() -> None:
    transport, requests = serve(
        page([1], '<?limit=2&cursor=A>; rel="next"'),
        page([], '<?limit=2&cursor=B>; rel="next"'),
        page([2]),
    )
    report = run(config(), transport=transport)
    assert report.exit_code == 0
    assert report.summary.pages == 3
    assert len(requests) == 3


def test_json_body_field_is_not_a_fallback_for_a_missing_link() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [], "next_cursor": "?limit=2&cursor=A"})

    report = run(config(), transport=httpx.MockTransport(handle))
    assert report.exit_code == 0
    assert report.summary.pages == 1


@pytest.mark.parametrize("ids", [[], [1]])
def test_self_loop_stops_without_a_completeness_claim(ids: list[int]) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oracle":
            return httpx.Response(200, json=[1, 2, 3])
        return page(ids, '<?limit=2&cursor=A>; rel="next"')

    report = run(
        config(oracle={"url": "https://api.test/oracle"}), transport=httpx.MockTransport(handle)
    )
    assert report.exit_code == 1
    assert report.summary.pages == 2
    assert report.trace.traversals[0].stop == "cycle"
    codes = {finding.code for finding in report.findings}
    assert "CP001" in codes and "CP003" not in codes
    assert analyze(parse_trace(report.trace.model_dump_json().encode())) == report


def test_multi_page_cycle_and_repeated_cursor_with_progress() -> None:
    link_a, link_b = '<?limit=2&cursor=A>; rel="next"', '<?limit=2&cursor=B>; rel="next"'
    transport, _ = serve(page([1], link_a), page([2], link_b), page([3], link_a), page([2], link_b))
    report = run(config(), transport=transport)
    assert report.exit_code == 1
    assert report.trace.traversals[0].stop == "cycle"
    assert {finding.code for finding in report.findings} == {"CP002", "CP006"}
    # The same cursor with new identities is progress, not a cycle.
    transport, _ = serve(page([1], link_a), page([2], link_a), page([3]))
    assert run(config(), transport=transport).exit_code == 0


def test_page_budget_is_incomplete() -> None:
    counter = iter(range(1000))

    def handle(request: httpx.Request) -> httpx.Response:
        number = next(counter)
        return page([number], f'<?limit=2&cursor={number}>; rel="next"')

    report = run(config(max_pages=3), transport=httpx.MockTransport(handle))
    assert report.exit_code == 2
    assert report.summary.pages == 3
    assert report.trace.traversals[0].stop == "budget"


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        ("https://other.test/orders/?limit=2&cursor=private", "origin"),
        ("//other.test/orders/?limit=2&cursor=private", "origin"),
        ("http://api.test/orders/?limit=2&cursor=private", "origin"),
        ("https://api.test:8443/orders/?limit=2&cursor=private", "origin"),
        ("https://user:password@api.test/orders/?limit=2", "malformed"),
        ("/other/?limit=2&cursor=private", "endpoint path"),
        ("/orders?limit=2&cursor=private", "endpoint path"),
        ("other?limit=2&cursor=private", "endpoint path"),
        ("/orders/../orders/?limit=2", "malformed"),
        ("ftp://api.test/orders/?limit=2", "malformed"),
        ("?limit=3&tenant=a&cursor=private", "scope parameters and page size"),
        ("?tenant=a&cursor=private", "scope parameters and page size"),
        ("?limit=2&tenant=b&cursor=private", "scope parameters and page size"),
        ("?limit=2&cursor=private", "scope parameters and page size"),
        ("?limit=2&tenant=a&extra=1&cursor=private", "scope parameters and page size"),
        ("?limit=2&tenant=a&cursor=private&after=x", "scope parameters and page size"),
    ],
)
def test_disallowed_target_receives_no_request_or_credentials(target: str, reason: str) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert len(requests) == 1, "Disallowed Link target received an HTTP request"
        assert request.headers["authorization"] == "Bearer private"
        return page([1], f'<{target}>; rel="next"')

    report = run(
        config(
            url="https://api.test/orders/?tenant=a", headers={"Authorization": "Bearer private"}
        ),
        transport=httpx.MockTransport(handle),
    )
    assert report.exit_code == 2
    assert len(requests) == 1
    assert reason in report.errors[0].message
    assert not report.findings
    saved = report.model_dump_json()
    for raw in ("private", "password", "other.test"):
        assert raw not in saved


def test_allowed_same_origin_target_keeps_credentials_and_cookies() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={"results": [{"id": 1}]},
                headers=[
                    ("Link", '<?limit=2&api_key=k3y&cursor=opaque%3D>; rel="next"'),
                    ("Set-Cookie", "session=c00kie; Path=/"),
                ],
            )
        return page([2])

    report = run(
        config(url="https://api.test/orders/?api_key=k3y", headers={"X-Api-Key": "hdr-credential"}),
        transport=httpx.MockTransport(handle),
    )
    assert report.exit_code == 0
    assert requests[1].url.query == b"limit=2&api_key=k3y&cursor=opaque%3D"
    assert requests[1].headers["x-api-key"] == "hdr-credential"
    assert requests[1].headers["cookie"] == "session=c00kie"
    saved = report.model_dump_json()
    for raw in ("k3y", "opaque", "hdr-credential", "c00kie"):
        assert raw not in saved


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_not_followed(status: int) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status,
            headers=[
                ("Location", "https://other.test/orders/"),
                ("Link", '<?limit=2&cursor=A>; rel="next"'),
            ],
        )

    report = run(config(), transport=httpx.MockTransport(handle))
    assert report.exit_code == 2
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"pagination": {"continuation": "link-header"}}, "requires pagination.mode: url"),
        ({"pagination": {"mode": "token", "continuation": "link-header"}}, "requires pagination"),
        ({"pagination": {**LINK, "continuation": "header"}}, "link-header"),
        ({"response": {"next_cursor": "$.next"}}, "conflicts with"),
        ({"response": {"next_cursor": "$.next_cursor"}}, "conflicts with"),
        ({"method": "POST", "body": {}}, "POST"),
        ({"pagination": {**LINK, "terminal_values": [""]}}, "terminal_values"),
        (
            {"cursor_binding": {"parameters": {"t": ["b"]}}, "parameters": {"t": "a"}},
            "cursor_binding",
        ),
    ],
)
def test_conflicting_configuration_is_rejected(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        config(**changes)


def test_configuration_fingerprint_separates_sources() -> None:
    json_url = Config.model_validate(
        {"url": "https://api.test/orders/", "limits": [2], "pagination": {"mode": "url"}}
    )
    explicit = Config.model_validate(
        {
            "url": "https://api.test/orders/",
            "limits": [2],
            "pagination": {"mode": "url", "continuation": "json"},
        }
    )
    assert json_url.pagination.continuation == "json"
    # Fingerprints of existing JSON URL-mode and token-mode configurations are unchanged.
    assert (
        replay_fingerprint(json_url)
        == replay_fingerprint(explicit)
        == "fb98cae9959edd3423b74d8779f37478b5aa0be59fb307092e7d25826c3f2e7e"
    )
    assert (
        replay_fingerprint(Config(url="https://api.test/orders", limits=[2]))
        == "74210238b8ec4402907c12fec4a6488b4146bd5c3913c27cfe02e5dc9c83f075"
    )
    assert replay_fingerprint(config()) != replay_fingerprint(json_url)


def write_config(path: Path, value: Config) -> None:
    path.write_text(value.model_dump_json(exclude_unset=True))


def test_cli_run_offline_replay_and_live_replay_contract(tmp_path: Path, monkeypatch) -> None:
    config_path, trace_path = tmp_path / "config.json", tmp_path / "trace.json"
    write_config(config_path, config())
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "cursor" in request.url.params:
            return page([2])
        return page([1], '</orders/?limit=2&cursor=A>; rel="next"')

    monkeypatch.setattr(
        "cursorproof.cli.execute", lambda cfg, **kw: run(cfg, transport=httpx.MockTransport(handle))
    )
    runner = CliRunner()
    assert runner.invoke(app, ["check", str(config_path)]).exit_code == 0
    result = runner.invoke(app, ["run", str(config_path), "--repro", str(trace_path)])
    assert result.exit_code == 0
    assert "CursorProof: PASS" in result.stdout
    assert len(calls) == 2
    recorded = json.loads(trace_path.read_text())
    assert recorded["continuation_source"] == "link-header"
    assert recorded["replay_fingerprint"] == replay_fingerprint(config())

    offline = runner.invoke(app, ["replay", str(trace_path), "--format", "json"])
    assert offline.exit_code == 0
    assert len(calls) == 2  # Offline replay sends nothing.
    assert json.loads(offline.stdout)["summary"]["unique_items"] == 2

    live = runner.invoke(app, ["replay", str(trace_path), "--config", str(config_path)])
    assert live.exit_code == 0
    assert len(calls) == 4

    # The same endpoint read through the JSON body is a different replay contract.
    write_config(
        config_path,
        Config.model_validate(
            {"url": "https://api.test/orders/", "limits": [2], "pagination": {"mode": "url"}}
        ),
    )
    changed = runner.invoke(
        app, ["replay", str(trace_path), "--config", str(config_path), "--format", "json"]
    )
    assert changed.exit_code == 2
    assert "contract does not match" in changed.stdout
    assert len(calls) == 4


def test_cli_reports_parse_failure_as_incomplete(tmp_path: Path, monkeypatch) -> None:
    config_path, trace_path = tmp_path / "config.json", tmp_path / "trace.json"
    write_config(config_path, config())
    transport, requests = serve(page([1], '<?limit=2&cursor=A>; rel="next"; anchor="/x"'))
    monkeypatch.setattr("cursorproof.cli.execute", lambda cfg, **kw: run(cfg, transport=transport))
    runner = CliRunner()
    result = runner.invoke(app, ["run", str(config_path), "--repro", str(trace_path)])
    assert result.exit_code == 2
    assert "anchor parameter" in result.stdout
    assert "Traversal did not complete (error)" in result.stdout
    assert runner.invoke(app, ["replay", str(trace_path)]).exit_code == 2


def test_cli_explains_configuration_conflict(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text(
        "url: https://api.test/orders/\n"
        "pagination: {mode: url, continuation: link-header}\n"
        "response: {next_cursor: $.next}\n"
    )
    result = CliRunner().invoke(app, ["check", str(path)])
    assert result.exit_code == 2
    assert "response.next_cursor is a JSON path and conflicts" in " ".join(result.output.split())


def link_trace() -> dict[str, object]:
    transport, _ = serve(page([1], '<?limit=2&cursor=A>; rel="next"'), page([2]))
    return run(config(), transport=transport).trace.model_dump(mode="json")


def test_replay_requires_link_evidence_for_each_page() -> None:
    trace = link_trace()
    assert analyze(parse_trace(json.dumps(trace).encode())).outcome == "pass"
    trace["traversals"][0]["pages"][1]["link"] = None  # type: ignore[index]
    report = analyze(parse_trace(json.dumps(trace).encode()))
    assert report.outcome == "error"
    assert "termination is unverified" in report.errors[0].message


@pytest.mark.parametrize(
    ("page_index", "evidence"),
    [
        (0, {"field_lines": 1, "links": 1, "next_links": 0}),
        (1, {"field_lines": 1, "links": 1, "next_links": 1}),
        (0, {"field_lines": 1, "links": 1, "next_links": 2}),
    ],
)
def test_replay_rejects_evidence_that_contradicts_the_continuation(
    page_index: int, evidence: dict[str, int]
) -> None:
    trace = link_trace()
    trace["traversals"][0]["pages"][page_index]["link"] = evidence  # type: ignore[index]
    report = analyze(parse_trace(json.dumps(trace).encode()))
    assert report.outcome == "error"
    assert "disagrees with the recorded continuation" in report.errors[0].message


def test_old_and_json_traces_are_never_read_as_link_traces() -> None:
    trace = link_trace()
    # A trace written before this field existed describes a JSON continuation.
    del trace["continuation_source"]
    old = parse_trace(json.dumps(trace).encode())
    assert old.continuation_source == "json"
    report = analyze(old)
    assert report.outcome == "error"
    assert "Link header evidence for a JSON continuation" in report.errors[0].message

    saved = Path(__file__).parents[1] / "docs" / "drf-control-500-trace.json"
    legacy = parse_trace(saved.read_bytes())
    assert legacy.continuation_source == "json"
    assert analyze(legacy).outcome == "pass"
    relabelled = legacy.model_copy(update={"continuation_source": "link-header"})
    assert analyze(relabelled).outcome == "error"


def test_json_url_mode_ignores_link_headers() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [{"id": 1}], "next": None},
            headers={"Link": '<https://other.test/orders/?limit=2>; rel="next"'},
        )

    report = run(
        Config.model_validate(
            {
                "url": "https://api.test/orders/",
                "limits": [2],
                "pagination": {"mode": "url"},
                "response": {"next_cursor": "$.next"},
            }
        ),
        transport=httpx.MockTransport(handle),
    )
    assert report.exit_code == 0
    assert report.trace.continuation_source == "json"
    assert report.trace.traversals[0].pages[0].link is None


def test_post_pagination_ignores_link_headers() -> None:
    bodies: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"results": [{"id": len(bodies)}], "next_cursor": 7 if len(bodies) == 1 else None},
            headers={"Link": '<https://other.test/x>; rel="next"'},
        )

    report = run(
        Config.model_validate(
            {"url": "https://api.test/scroll", "method": "POST", "body": {}, "limits": [2]}
        ),
        transport=httpx.MockTransport(handle),
    )
    assert report.exit_code == 0
    assert bodies == [{"limit": 2}, {"limit": 2, "cursor": 7}]


def test_registry_example_is_valid_and_follows_tag_links(tmp_path: Path, monkeypatch) -> None:
    example = EXAMPLES / "registry-tags.yml"
    parsed, _ = load_config(example)
    assert parsed.link_continuation
    assert CliRunner().invoke(app, ["check", str(example)]).exit_code == 0
    tags = ["alpha", "beta", "delta", "epsilon", "gamma"]

    def handle(request: httpx.Request) -> httpx.Response:
        size = int(request.url.params["n"])
        rest = [tag for tag in tags if tag > request.url.params.get("last", "")]
        headers = (
            {"Link": f'<{request.url.path}?last={rest[size - 1]}&n={size}>; rel="next"'}
            if len(rest) > size
            else {}
        )
        return httpx.Response(200, json={"name": "example", "tags": rest[:size]}, headers=headers)

    monkeypatch.setattr(
        "cursorproof.cli.execute", lambda cfg, **kw: run(cfg, transport=httpx.MockTransport(handle))
    )
    trace_path = tmp_path / "trace.json"
    result = CliRunner().invoke(
        app, ["run", str(example), "--format", "json", "--repro", str(trace_path)]
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["summary"]["unique_items"] == 5
    for traversal in report["trace"]["traversals"]:
        assert [item["id"] for p in traversal["pages"] for item in p["items"]] == tags
