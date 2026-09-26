import json
import math
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

import httpx

from cursorproof import __version__
from cursorproof.checks import analyze, identity_key
from cursorproof.config import Config, Identity, Oracle, Ordering, Parameter
from cursorproof.models import (
    BindingObservation,
    Issue,
    Item,
    MutationEvent,
    Page,
    Report,
    Trace,
    Traversal,
)
from cursorproof.paths import extract
from cursorproof.privacy import Redactor

type SortValue = str | int | float | datetime | None


class ExecutionError(ValueError):
    """A safe diagnostic, containing no response bodies or credentials."""


@dataclass
class RawItem:
    item: Item
    values: list[SortValue]


def identity(value: object) -> Identity:
    if type(value) not in {str, int}:
        raise ExecutionError("Item identity must be a string or integer (not null or boolean)")
    return cast(Identity, value)


def sort_value(value: object, ordering: Ordering) -> SortValue:
    if value is None:
        return None
    if ordering.type == "datetime":
        if not isinstance(value, str):
            raise ExecutionError("Datetime ordering requires ISO 8601 strings with timezone")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ExecutionError("Invalid ISO 8601 ordering value") from None
        if parsed.tzinfo is None:
            raise ExecutionError("Datetime ordering requires a timezone")
        return parsed
    if type(value) not in {str, int, float}:
        raise ExecutionError("Ordering values must be strings, finite numbers, or null")
    if isinstance(value, float) and not math.isfinite(value):
        raise ExecutionError("Non-finite ordering value")
    if ordering.type == "string" and not isinstance(value, str):
        raise ExecutionError("String ordering received a non-string value")
    if ordering.type == "number" and not isinstance(value, (int, float)):
        raise ExecutionError("Numeric ordering received a non-numeric value")
    return cast(SortValue, value)


def assign_ranks(rows: list[RawItem], ordering: list[Ordering]) -> None:
    """Persist order-preserving ranks rather than possibly sensitive sort values."""
    ranks: list[dict[SortValue, int]] = []
    for index, field in enumerate(ordering):
        values = {row.values[index] for row in rows if row.values[index] is not None}
        try:
            ordered = sorted(values, reverse=field.direction == "desc")  # type: ignore[type-var]
        except TypeError:
            raise ExecutionError("An ordering field mixes incompatible value types") from None
        rank = {value: position for position, value in enumerate(ordered)}
        rank[None] = -1 if field.nulls == "first" else len(ordered)
        ranks.append(rank)
    for row in rows:
        row.item.sort_key = [rank[row.values[index]] for index, rank in enumerate(ranks)]


def decode_json(body: bytes) -> object:
    def invalid_constant(value: str) -> object:
        raise ValueError("Non-finite JSON number")

    try:
        return json.loads(body, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ExecutionError("Response is not valid finite JSON") from None


def fetch(client: httpx.Client, request: httpx.Request, max_bytes: int) -> tuple[int, bytes]:
    try:
        response = client.send(request, stream=True)
        try:
            body = bytearray()
            for chunk in response.iter_bytes():
                if len(body) + len(chunk) > max_bytes:
                    raise ExecutionError("Response exceeded max_response_bytes")
                body.extend(chunk)
            return response.status_code, bytes(body)
        finally:
            response.close()
    except httpx.HTTPError:
        raise ExecutionError("HTTP request failed or timed out") from None


def command_output(command: list[str], timeout: float, cwd: Path, max_bytes: int) -> bytes:
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
            if result.returncode != 0:
                raise ExecutionError("Oracle command failed")
            output.seek(0)
            body = output.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ExecutionError("Oracle output exceeded max_response_bytes")
            return body
    except (OSError, subprocess.TimeoutExpired):
        raise ExecutionError("Oracle command could not execute or timed out") from None


def read_oracle(
    oracle: Oracle,
    config: Config,
    cwd: Path,
    transport: httpx.BaseTransport | None,
) -> list[Identity]:
    if oracle.command is not None:
        body = command_output(oracle.command, oracle.timeout, cwd, config.max_response_bytes)
    else:
        # A separate client avoids leaking endpoint headers or cookies to the oracle.
        with httpx.Client(timeout=oracle.timeout, transport=transport) as client:
            request = client.build_request("GET", cast(str, oracle.url), headers=oracle.headers)
            status, body = fetch(client, request, config.max_response_bytes)
        if not 200 <= status < 300:
            raise ExecutionError(f"Oracle returned HTTP {status}")
    if oracle.format == "lines":
        try:
            values: object = body.decode("utf-8").splitlines()
        except UnicodeError:
            raise ExecutionError("Lines oracle must use UTF-8") from None
    else:
        values = extract(decode_json(body), oracle.items)
    if not isinstance(values, list):
        raise ExecutionError("Oracle items must be an array")
    if len(values) > config.max_items:
        raise ExecutionError("Oracle exceeded max_items")
    result = [identity(extract(value, oracle.id)) for value in values]
    if len({identity_key(value) for value in result}) != len(result):
        raise ExecutionError("Oracle contains duplicate identities")
    return result


def run_mutations(config: Config, page: int, cwd: Path, trace: Trace) -> None:
    for hook in config.mutations:
        if hook.after_page != page:
            continue
        try:
            result = subprocess.run(
                hook.command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=hook.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ExecutionError("Mutation hook could not execute or timed out") from None
        if result.returncode:
            raise ExecutionError("Mutation hook failed")
        trace.mutations.append(MutationEvent(after_page=page))


def probe_binding(
    client: httpx.Client,
    config: Config,
    cursor: str,
    limit: int,
    trace: Trace,
    redactor: Redactor,
) -> None:
    binding = config.cursor_binding
    assert binding is not None
    for name, alternatives in binding.parameters.items():
        for alternative in alternatives:
            params: dict[str, Parameter] = {
                **config.parameters,
                name: alternative,
                config.pagination.limit_param: limit,
                config.pagination.cursor_param: cursor,
            }
            request = build_request(client, config.url, params)
            status, _ = fetch(client, request, config.max_response_bytes)
            trace.bindings.append(
                BindingObservation(
                    parameter=redactor.text(name),
                    request=redactor.url(str(request.url), config.pagination.cursor_param),
                    cursor=cast(str, redactor.cursor(cursor)),
                    status=status,
                    accepted_rejection=status in binding.reject_statuses,
                )
            )


def build_request(client: httpx.Client, url: str, params: dict[str, Parameter]) -> httpx.Request:
    merged = httpx.URL(url).params.merge(params)
    return client.build_request("GET", url, params=merged)


def traverse(
    client: httpx.Client,
    config: Config,
    traversal: Traversal,
    trace: Trace,
    redactor: Redactor,
    cwd: Path,
    run_number: int,
) -> None:
    cursor: str | None = None
    seen: set[str] = set()
    rows: list[RawItem] = []
    current_page = 1
    try:
        for current_page in range(1, config.max_pages + 1):
            params: dict[str, Parameter] = {
                **config.parameters,
                config.pagination.limit_param: traversal.limit,
            }
            if cursor is not None:
                params[config.pagination.cursor_param] = cursor
            request = build_request(client, config.url, params)
            status, body = fetch(client, request, config.max_response_bytes)
            if not 200 <= status < 300:
                raise ExecutionError(f"Traversal returned HTTP {status}")
            payload = decode_json(body)
            values = extract(payload, config.response.items)
            raw_cursor = extract(payload, config.response.next_cursor)
            if raw_cursor is not None and not isinstance(raw_cursor, str):
                raise ExecutionError("Cursor must be a string or null")
            if raw_cursor is None and None not in config.pagination.terminal_values:
                raise ExecutionError("Null cursor is not declared as a terminal value")
            next_cursor = None if raw_cursor in config.pagination.terminal_values else raw_cursor
            has_more = (
                extract(payload, config.response.has_more)
                if config.response.has_more is not None
                else None
            )
            if config.response.has_more is not None and type(has_more) is not bool:
                raise ExecutionError("has_more must be a boolean")
            if not isinstance(values, list):
                raise ExecutionError("Response items must be an array")
            if len(rows) + len(values) > config.max_items:
                traversal.stop = "budget"
                raise ExecutionError("Traversal exceeded max_items")
            page_rows = [
                RawItem(
                    item=Item(id=redactor.identity(identity(extract(value, config.response.id)))),
                    values=[
                        sort_value(extract(value, field.field), field) for field in config.ordering
                    ],
                )
                for value in values
            ]
            rows.extend(page_rows)
            traversal.pages.append(
                Page(
                    number=current_page,
                    request=redactor.url(str(request.url), config.pagination.cursor_param),
                    cursor=redactor.cursor(cursor),
                    next_cursor=redactor.cursor(next_cursor),
                    has_more=cast(bool | None, has_more),
                    status=status,
                    items=[row.item for row in page_rows],
                )
            )
            if next_cursor is None:
                traversal.stop = "terminal"
                break
            if next_cursor in seen:
                traversal.stop = "cycle"
                break
            seen.add(next_cursor)
            if current_page == config.max_pages:
                traversal.stop = "budget"
                break
            if config.cursor_binding and not trace.bindings:
                probe_binding(client, config, next_cursor, traversal.limit, trace, redactor)
            run_mutations(config, current_page, cwd, trace)
            cursor = next_cursor
    except (ExecutionError, ValueError, httpx.InvalidURL) as exc:
        if traversal.stop != "budget":
            traversal.stop = "error"
        message = (
            str(exc) if isinstance(exc, ExecutionError) else "Response/configuration is invalid"
        )
        trace.errors.append(Issue(message=message, traversal=run_number, page=current_page))
    try:
        assign_ranks(rows, config.ordering)
    except ExecutionError as exc:
        trace.errors.append(Issue(message=str(exc), traversal=run_number))


def run(
    config: Config,
    *,
    cwd: Path | None = None,
    secrets: set[str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Report:
    cwd = cwd or Path.cwd()
    secret_values = set(secrets or ()) | set(config.headers.values())
    if config.oracle:
        secret_values.update(config.oracle.headers.values())
    redactor = Redactor(secret_values)
    trace = Trace(
        tool_version=__version__,
        consistency=config.consistency,
        ordering_fields=[redactor.text(field.field) for field in config.ordering],
        traversals=[
            Traversal(limit=limit, repetition=repetition)
            for limit in config.limits
            for repetition in range(1, config.repeats + 1)
        ],
    )
    if config.oracle is None:
        trace.notes.append("No oracle: completeness is not established.")
    if config.consistency == "static":
        trace.notes.append(
            "Static contract: the dataset must remain unchanged across all traversals."
        )
    elif config.consistency == "snapshot":
        trace.notes.append("Snapshot oracle must describe the API's initial snapshot.")
    else:
        trace.notes.append(
            "Live keyset checks uniqueness and order with immutable keys/identities; "
            "it does not establish completeness under concurrent writes."
        )
    if config.oracle:
        try:
            trace.oracle = [
                redactor.identity(value)
                for value in read_oracle(
                    config.oracle,
                    config,
                    cwd,
                    transport,
                )
            ]
            trace.oracle_ordered = config.oracle.ordered
        except (ExecutionError, ValueError, OSError, httpx.HTTPError, httpx.InvalidURL) as exc:
            message = str(exc) if isinstance(exc, ExecutionError) else "Oracle response is invalid"
            trace.errors.append(Issue(message=message))
            return analyze(trace)
    try:
        with httpx.Client(
            headers=config.headers,
            timeout=config.timeout,
            transport=transport,
            follow_redirects=False,
        ) as client:
            for number, traversal in enumerate(trace.traversals, 1):
                traverse(client, config, traversal, trace, redactor, cwd, number)
    except (OSError, ValueError, httpx.HTTPError, httpx.InvalidURL):
        trace.errors.append(
            Issue(message="Cannot initialize HTTP client or send configured request")
        )
    if config.cursor_binding and not trace.bindings:
        trace.errors.append(Issue(message="Binding probes could not obtain a usable cursor"))
    if len(trace.mutations) != len(config.mutations):
        trace.errors.append(Issue(message="Not all configured mutation hooks were reached"))
    return analyze(trace)
