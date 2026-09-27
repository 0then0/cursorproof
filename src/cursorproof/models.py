from typing import Literal

from pydantic import Field

from cursorproof.config import Identity, Model, Positive


class Item(Model):
    id: Identity
    sort_key: list[int] | None = None
    fingerprint: str | None = None


class SnapshotItem(Model):
    id: Identity
    fingerprint: str


class Page(Model):
    number: Positive
    request: str
    cursor: str | None
    next_cursor: str | None
    has_more: bool | None = None
    status: int = 200
    items: list[Item]


class Traversal(Model):
    limit: Positive
    repetition: Positive = 1
    pages: list[Page] = Field(default_factory=list)
    stop: Literal["terminal", "cycle", "budget", "error"] = "error"


class BindingObservation(Model):
    parameter: str
    case: Positive
    phase: Literal["baseline", "cursor"]
    request: str
    cursor: str | None = None
    status: int
    valid: bool = True


class Issue(Model):
    message: str
    traversal: int | None = None
    page: int | None = None


class MutationEvent(Model):
    after_page: Positive


class Trace(Model):
    schema_version: Literal[2] = 2
    tool_version: str
    replay_fingerprint: str | None = None
    consistency: Literal["static", "snapshot", "live-keyset"]
    ordering_fields: list[str] = Field(default_factory=list)
    traversals: list[Traversal] = Field(min_length=1)
    oracle: list[Identity] | None = None
    oracle_ordered: bool = True
    oracle_snapshot: list[SnapshotItem] | None = None
    binding_reject_statuses: list[int] = Field(default_factory=lambda: [400, 409, 422])
    binding_cases_expected: int = Field(default=0, ge=0)
    bindings: list[BindingObservation] = Field(default_factory=list)
    mutations: list[MutationEvent] = Field(default_factory=list)
    errors: list[Issue] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Location(Model):
    traversal: Positive
    page: Positive
    position: Positive | None = None


class Finding(Model):
    code: str
    name: str
    message: str
    locations: list[Location] = Field(default_factory=list)
    item_ids: list[Identity] = Field(default_factory=list)
    count: int = 1
    possible_cause: str | None = None
    requests: list[str] = Field(default_factory=list)


class Summary(Model):
    traversals: int
    pages: int
    responses: int
    items: int
    unique_items: int


class Report(Model):
    schema_version: Literal[2] = 2
    outcome: Literal["pass", "fail", "error"]
    summary: Summary
    findings: list[Finding]
    errors: list[Issue]
    notes: list[str]
    trace: Trace

    @property
    def exit_code(self) -> int:
        return {"pass": 0, "fail": 1, "error": 2}[self.outcome]


def parse_trace(data: bytes) -> Trace:
    import json

    try:
        value = json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Trace is not valid JSON") from None
    if not isinstance(value, dict):
        raise ValueError("Trace must be a JSON object")
    version = value.get("schema_version")
    if version is None:
        raise ValueError("Trace schema version is missing")
    if type(version) is int and version == 1:
        legacy = dict(value)
        legacy["schema_version"] = 2
        legacy["bindings"] = []
        raw_errors = legacy.get("errors", [])
        errors = list(raw_errors) if isinstance(raw_errors, list) else []
        if not isinstance(raw_errors, list):
            errors.append({"message": "Legacy trace errors field is invalid"})
        if legacy.get("consistency") == "snapshot":
            errors.append({"message": "Legacy snapshot trace lacks full-record snapshot evidence"})
        raw_bindings = value.get("bindings", [])
        if not isinstance(raw_bindings, list):
            errors.append({"message": "Legacy trace bindings field is invalid"})
        elif raw_bindings:
            errors.append({"message": "Legacy binding trace lacks a successful baseline response"})
            legacy["binding_cases_expected"] = len(raw_bindings)
        legacy["errors"] = errors
        return Trace.model_validate(legacy)
    return Trace.model_validate(value)
