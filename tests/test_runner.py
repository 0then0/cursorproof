from pathlib import Path

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cursorproof.checks import analyze
from cursorproof.config import Config
from cursorproof.models import Trace
from cursorproof.runner import run


def config(**changes: object) -> Config:
    return Config.model_validate({"url": "https://api.test/orders", "limits": [2], **changes})


def scripted(pages: list[dict[str, object]]) -> httpx.MockTransport:
    responses = iter(pages)
    return httpx.MockTransport(lambda request: httpx.Response(200, json=next(responses)))


def page(ids: list[str | int], cursor: str | None = None, **other: object) -> dict[str, object]:
    return {"results": [{"id": item} for item in ids], "next_cursor": cursor, **other}


def codes(report: object) -> set[str]:
    return {finding.code for finding in report.findings}


def test_duplicate_has_exact_locations_and_replays() -> None:
    report = run(config(), transport=scripted([page([1, 2], "A"), page([2, 3])]))
    assert report.exit_code == 1
    finding = report.findings[0]
    assert finding.code == "CP002"
    assert [(loc.page, loc.position) for loc in finding.locations] == [(1, 2), (2, 1)]
    trace = Trace.model_validate_json(report.trace.model_dump_json())
    assert analyze(trace) == report


@pytest.mark.parametrize(("cursors", "code"), [(["A", "A"], "CP001"), (["A", "B", "A"], "CP006")])
def test_cursor_cycles_stop(cursors: list[str], code: str) -> None:
    report = run(config(), transport=scripted([page([i], c) for i, c in enumerate(cursors)]))
    assert report.exit_code == 1
    assert codes(report) == {code}
    assert report.summary.pages == len(cursors)


def test_empty_nonterminal_page_and_empty_string_cursor() -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=page([], "") if len(requests) == 1 else page([1]))

    report = run(config(), transport=httpx.MockTransport(handle))
    assert report.exit_code == 0
    assert requests[1].url.params["cursor"] == ""


def test_explicit_empty_terminal_cursor() -> None:
    report = run(
        config(pagination={"terminal_values": [None, ""]}),
        transport=scripted([page([1], "")]),
    )
    assert report.exit_code == 0
    assert report.summary.pages == 1


def test_duplicate_within_page_and_typed_identities() -> None:
    report = run(config(limits=[5]), transport=scripted([page([1, "1", 1])]))
    assert codes(report) == {"CP002"}
    assert report.summary.unique_items == 2


@pytest.mark.parametrize("bad_id", [None, True, False, 1.0, {}, []])
def test_invalid_id_is_error(bad_id: object) -> None:
    report = run(config(), transport=scripted([{"results": [{"id": bad_id}], "next_cursor": None}]))
    assert report.exit_code == 2


def test_order_across_pages_and_null_policy() -> None:
    report = run(
        config(ordering=[{"field": "id", "direction": "desc"}]),
        transport=scripted([page([4, 3], "A"), page([5, 1])]),
    )
    assert codes(report) == {"CP004"}
    assert [loc.page for loc in report.findings[0].locations] == [1, 2]


def test_compound_order_and_nulls() -> None:
    rows = [{"id": 4, "group": 3}, {"id": 3, "group": 3}, {"id": 2, "group": None}]
    report = run(
        config(
            limits=[3],
            ordering=[
                {"field": "group", "direction": "desc", "nulls": "last"},
                {"field": "id", "direction": "desc"},
            ],
        ),
        transport=scripted([{"results": rows, "next_cursor": None}]),
    )
    assert report.exit_code == 0
    rows[0], rows[1] = rows[1], rows[0]
    report = run(
        config(
            limits=[3],
            ordering=[
                {"field": "group", "direction": "desc", "nulls": "last"},
                {"field": "id", "direction": "desc"},
            ],
        ),
        transport=scripted([{"results": rows, "next_cursor": None}]),
    )
    assert codes(report) == {"CP004"}


def test_datetime_compares_instants_not_strings() -> None:
    rows = [
        {"id": 1, "time": "2026-01-01T01:00:00+02:00"},
        {"id": 2, "time": "2026-01-01T00:00:00+00:00"},
    ]
    report = run(
        config(ordering=[{"field": "time", "type": "datetime"}]),
        transport=scripted([{"results": rows, "next_cursor": None}]),
    )
    assert report.exit_code == 0


def test_mixed_order_types_are_inconclusive() -> None:
    report = run(config(ordering=[{"field": "id"}]), transport=scripted([page([1, "2"])]))
    assert report.exit_code == 2
    assert "CP004" not in codes(report)


@pytest.mark.parametrize(
    "payload",
    [
        {"results": []},
        {"results": [], "next_cursor": 0},
        {"results": {}, "next_cursor": None},
        {"results": [], "next_cursor": None, "has_more": "false"},
    ],
)
def test_invalid_response_never_passes(payload: dict[str, object]) -> None:
    report = run(config(response={"has_more": "$.has_more"}), transport=scripted([payload]))
    assert report.exit_code == 2


def test_termination_and_page_size() -> None:
    report = run(
        config(response={"has_more": "$.has_more"}),
        transport=scripted([page([1, 2, 3], has_more=True)]),
    )
    assert codes(report) == {"CP005", "CP008"}


def test_max_pages_is_not_success_or_missing_items() -> None:
    report = run(config(max_pages=1), transport=scripted([page([1], "A")]))
    assert report.exit_code == 2
    assert not report.findings


def test_max_items_and_body_limit() -> None:
    report = run(config(max_items=1), transport=scripted([page([1, 2])]))
    assert report.exit_code == 2
    report = run(config(max_response_bytes=1), transport=scripted([page([])]))
    assert report.exit_code == 2


@pytest.mark.parametrize("status", [301, 401, 429, 500])
def test_http_errors_are_not_pagination_findings(status: int) -> None:
    report = run(config(), transport=httpx.MockTransport(lambda r: httpx.Response(status)))
    assert report.exit_code == 2
    assert not report.findings


def test_http_timeout_and_invalid_json() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret response data", request=request)

    report = run(config(), transport=httpx.MockTransport(timeout))
    assert report.exit_code == 2
    assert "secret response data" not in report.model_dump_json()
    report = run(
        config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))
    )
    assert report.exit_code == 2


def test_query_parameters_are_preserved() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.params["sort"] == "desc"
        assert request.url.params["status"] == "open"
        assert request.url.params["limit"] == "2"
        return httpx.Response(200, json=page([]))

    assert (
        run(
            config(url="https://api.test/orders?sort=desc", parameters={"status": "open"}),
            transport=httpx.MockTransport(handle),
        ).exit_code
        == 0
    )


def test_oracle_missing_extra_and_order() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[1, 2, 3] if request.url.path == "/oracle" else page([1, 4])
        )

    report = run(
        config(oracle={"url": "https://oracle.test/oracle"}), transport=httpx.MockTransport(handle)
    )
    assert codes(report) == {"CP003", "CP010"}
    assert report.findings[0].item_ids == [2, 3]


def test_missing_items_does_not_hide_shared_order_violation() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[1, 2, 3] if request.url.path == "/oracle" else page([3, 1]),
        )

    report = run(
        config(oracle={"url": "https://api.test/oracle"}),
        transport=httpx.MockTransport(handle),
    )
    assert codes(report) == {"CP003", "CP004"}
    missing = next(finding for finding in report.findings if finding.code == "CP003")
    assert missing.locations == []


def test_snapshot_requires_oracle_and_fields() -> None:
    with pytest.raises(ValueError, match="requires an oracle"):
        config(consistency="snapshot")
    with pytest.raises(ValueError, match="snapshot_fields"):
        config(consistency="snapshot", oracle={"url": "https://api.test/oracle"})
    with pytest.raises(ValueError, match="only valid with snapshot"):
        config(response={"snapshot_fields": ["$.status"]})


def test_oracle_unordered_and_duplicates() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2] if request.url.path == "/oracle" else page([2, 1]))

    ordered = run(
        config(oracle={"url": "https://api.test/oracle"}), transport=httpx.MockTransport(handle)
    )
    assert codes(ordered) == {"CP004"}
    unordered = run(
        config(oracle={"url": "https://api.test/oracle", "ordered": False}),
        transport=httpx.MockTransport(handle),
    )
    assert unordered.exit_code == 0
    duplicate = run(
        config(oracle={"url": "https://api.test/oracle"}),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[1, 1])),
    )
    assert duplicate.exit_code == 2


def test_oracle_does_not_receive_endpoint_credentials() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oracle":
            assert "authorization" not in request.headers
            return httpx.Response(200, json=[])
        assert request.headers["authorization"] == "Bearer token"
        return httpx.Response(200, json=page([]))

    report = run(
        config(
            headers={"Authorization": "Bearer token"}, oracle={"url": "https://api.test/oracle"}
        ),
        transport=httpx.MockTransport(handle),
    )
    assert report.exit_code == 0


def test_cross_limit_stream_and_repetitions() -> None:
    report = run(config(limits=[1, 2]), transport=scripted([page([1]), page([1, 2])]))
    assert codes(report) == {"CP009"}
    report = run(config(repeats=2), transport=scripted([page([1]), page([2])]))
    assert codes(report) == {"CP009"}


@pytest.mark.parametrize(("status", "exit_code"), [(400, 0), (200, 1), (500, 2), (403, 2)])
def test_binding_policy(status: int, exit_code: int) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.params["status"] == "closed":
            if "cursor" not in request.url.params:
                return httpx.Response(200, json=page([]))
            assert request.url.params["cursor"] == "A"
            return httpx.Response(status, json={})
        return httpx.Response(
            200, json=page([2]) if "cursor" in request.url.params else page([1], "A")
        )

    report = run(
        config(
            parameters={"status": "open"}, cursor_binding={"parameters": {"status": ["closed"]}}
        ),
        transport=httpx.MockTransport(handle),
    )
    assert report.exit_code == exit_code
    assert [item.phase for item in report.trace.bindings] == ["baseline", "cursor"]


def test_binding_no_cursor_is_incomplete() -> None:
    report = run(
        config(
            parameters={"status": "open"}, cursor_binding={"parameters": {"status": ["closed"]}}
        ),
        transport=scripted([page([])]),
    )
    assert report.exit_code == 2


def test_invalid_binding_alternative_does_not_count_as_rejection() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.params["status"] == "invalid":
            return httpx.Response(400, json={})
        return httpx.Response(
            200,
            json=page([1], "A") if "cursor" not in request.url.params else page([2]),
        )

    report = run(
        config(
            parameters={"status": "open"},
            cursor_binding={"parameters": {"status": ["invalid"]}},
        ),
        transport=httpx.MockTransport(handle),
    )
    assert report.exit_code == 2
    assert not report.findings
    assert any("invalid without a cursor" in error.message for error in report.errors)


def test_default_page_budget_handles_over_one_thousand_items() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        limit = int(request.url.params["limit"])
        start = int(request.url.params.get("cursor", "0"))
        stop = min(start + limit, 1001)
        return httpx.Response(
            200,
            json=page(list(range(start, stop)), str(stop) if stop < 1001 else None),
        )

    report = run(config(limits=[1]), transport=httpx.MockTransport(handle))
    assert report.exit_code == 0
    assert report.summary.pages == 1001


def test_secrets_and_raw_cursor_not_in_report() -> None:
    secret = "a/b+TOP-SECRET"
    report = run(
        config(
            headers={"Authorization": f"Bearer {secret}"},
            parameters={"token": secret},
            ordering=[{"field": "value"}],
        ),
        secrets={secret},
        transport=scripted(
            [
                {"results": [{"id": secret, "value": secret}], "next_cursor": "opaque-secret"},
                {"results": [{"id": secret, "value": secret}], "next_cursor": None},
            ]
        ),
    )
    output = report.model_dump_json()
    assert secret not in output
    assert "opaque-secret" not in output
    assert "Bearer" not in output
    assert codes(report) == {"CP002"}
    assert analyze(Trace.model_validate_json(report.trace.model_dump_json())) == report


def test_mutation_hook_runs_between_pages(tmp_path: Path) -> None:
    import sys

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oracle":
            return httpx.Response(200, json=[{"id": 1, "value": "a"}, {"id": 2, "value": "b"}])
        if "cursor" in request.url.params:
            assert (tmp_path / "changed").exists()
            return httpx.Response(
                200, json={"results": [{"id": 2, "value": "b"}], "next_cursor": None}
            )
        assert not (tmp_path / "changed").exists()
        return httpx.Response(200, json={"results": [{"id": 1, "value": "a"}], "next_cursor": "A"})

    report = run(
        config(
            consistency="snapshot",
            response={"snapshot_fields": ["$.value"]},
            oracle={"url": "https://api.test/oracle", "items": "$", "id": "$.id"},
            mutations=[
                {
                    "after_page": 1,
                    "command": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('changed').touch()",
                    ],
                }
            ],
        ),
        cwd=tmp_path,
        transport=httpx.MockTransport(handle),
    )
    assert report.exit_code == 0
    assert [event.after_page for event in report.trace.mutations] == [1]


def test_unreached_mutation_is_incomplete() -> None:
    import sys

    report = run(
        config(
            consistency="snapshot",
            response={"snapshot_fields": ["$.value"]},
            oracle={"command": [sys.executable, "-c", "print('[]')"]},
            mutations=[
                {
                    "after_page": 1,
                    "command": ["must-not-execute"],
                }
            ],
        ),
        transport=scripted([page([])]),
    )
    assert report.exit_code == 2
    assert not report.trace.mutations


def test_live_keyset_allows_unseen_insertions() -> None:
    report = run(
        config(
            consistency="live-keyset",
            immutable_ordering=True,
            ordering=[{"field": "id", "direction": "desc"}],
        ),
        transport=scripted([page([10, 9], "A"), page([7, 6])]),
    )
    assert report.exit_code == 0
    assert any("completeness" in note for note in report.notes)


@given(
    st.lists(st.integers(min_value=-100, max_value=100), unique=True, max_size=30),
    st.integers(min_value=1, max_value=12),
)
@settings(max_examples=50, deadline=None)
def test_valid_stream_is_independent_of_page_boundaries(ids: list[int], limit: int) -> None:
    ids = sorted(ids)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oracle":
            return httpx.Response(200, json=ids)
        size = int(request.url.params["limit"])
        start = int(request.url.params.get("cursor", "0"))
        end = start + size
        return httpx.Response(200, json=page(ids[start:end], str(end) if end < len(ids) else None))

    report = run(
        config(
            limits=sorted({1, limit, limit + 1}),
            ordering=[{"field": "id"}],
            oracle={"url": "https://api.test/oracle"},
        ),
        transport=httpx.MockTransport(handle),
    )
    assert report.exit_code == 0


@given(st.integers(min_value=1, max_value=20), st.integers(min_value=1, max_value=10))
@settings(max_examples=30, deadline=None)
def test_duplicate_at_generated_boundary_is_always_detected(size: int, limit: int) -> None:
    ids = list(range(size))
    pages = [page(ids[i : i + limit], str(i + limit)) for i in range(0, size, limit)]
    pages.append(page([ids[-1]]))
    report = run(config(limits=[limit]), transport=scripted(pages))
    assert "CP002" in codes(report)
