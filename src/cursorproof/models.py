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


class Issue(Model):
    message: str
    traversal: int | None = None
    page: int | None = None


class MutationEvent(Model):
    after_page: Positive


class Trace(Model):
    schema_version: Literal[1] = 1
    tool_version: str
    consistency: Literal["static", "snapshot", "live-keyset"]
    ordering_fields: list[str] = Field(default_factory=list)
    traversals: list[Traversal] = Field(min_length=1)
    oracle: list[Identity] | None = None
    oracle_ordered: bool = True
    oracle_snapshot: list[SnapshotItem] | None = None
    binding_reject_statuses: list[int] = Field(default_factory=lambda: [400, 409, 422])
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
    schema_version: Literal[1] = 1
    outcome: Literal["pass", "fail", "error"]
    summary: Summary
    findings: list[Finding]
    errors: list[Issue]
    notes: list[str]
    trace: Trace

    @property
    def exit_code(self) -> int:
        return {"pass": 0, "fail": 1, "error": 2}[self.outcome]
