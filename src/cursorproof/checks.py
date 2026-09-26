from cursorproof.config import Identity
from cursorproof.models import Finding, Issue, Location, Report, Summary, Trace


def identity_key(value: Identity) -> tuple[type[str] | type[int], Identity]:
    return type(value), value


def analyze(trace: Trace) -> Report:
    findings: list[Finding] = []
    errors = list(trace.errors)
    complete: list[tuple[int, list[Identity]]] = []
    all_ids: set[tuple[type[str] | type[int], Identity]] = set()
    total_items = 0
    for run_number, traversal in enumerate(trace.traversals, 1):
        seen_items: dict[tuple[type[str] | type[int], Identity], Location] = {}
        seen_cursors: dict[str, int] = {}
        previous: tuple[list[int], Location, Identity] | None = None
        stream: list[Identity] = []
        cursor: str | None = None
        cycle = False
        for expected_page, page in enumerate(traversal.pages, 1):
            here = Location(traversal=run_number, page=page.number)
            if page.number != expected_page or page.cursor != cursor:
                errors.append(Issue(message="Trace has inconsistent page/cursor linkage"))
            if not 200 <= page.status < 300:
                errors.append(Issue(message="Trace contains a non-successful traversal response"))
            if len(page.items) > traversal.limit:
                findings.append(
                    Finding(
                        code="CP005",
                        name="PAGE_SIZE_EXCEEDED",
                        message="Page contains more items than the requested limit.",
                        locations=[here],
                    )
                )
            if page.has_more is not None and page.has_more != (page.next_cursor is not None):
                findings.append(
                    Finding(
                        code="CP008",
                        name="TERMINATION_ERROR",
                        message="has_more disagrees with the configured terminal cursor.",
                        locations=[here],
                    )
                )
            for position, item in enumerate(page.items, 1):
                location = Location(traversal=run_number, page=page.number, position=position)
                key = identity_key(item.id)
                stream.append(item.id)
                all_ids.add(key)
                total_items += 1
                if key in seen_items:
                    findings.append(
                        Finding(
                            code="CP002",
                            name="DUPLICATE_ITEM",
                            message="Item identity was returned more than once in this traversal.",
                            locations=[seen_items[key], location],
                            item_ids=[item.id],
                        )
                    )
                else:
                    seen_items[key] = location
                if trace.ordering_fields:
                    if item.sort_key is None or len(item.sort_key) != len(trace.ordering_fields):
                        errors.append(
                            Issue(
                                message="Ordering could not be evaluated for an item",
                                traversal=run_number,
                                page=page.number,
                            )
                        )
                        previous = None
                    else:
                        if previous and item.sort_key < previous[0]:
                            findings.append(
                                Finding(
                                    code="CP004",
                                    name="ORDER_VIOLATION",
                                    message="Items violate the declared lexicographic ordering.",
                                    locations=[previous[1], location],
                                    item_ids=[previous[2], item.id],
                                )
                            )
                        previous = item.sort_key, location, item.id
            next_cursor = page.next_cursor
            if next_cursor is not None:
                if next_cursor == page.cursor:
                    cycle = True
                    findings.append(
                        Finding(
                            code="CP001",
                            name="CURSOR_NOT_ADVANCING",
                            message="The next cursor is identical to the request cursor.",
                            locations=[here],
                        )
                    )
                elif next_cursor in seen_cursors:
                    cycle = True
                    findings.append(
                        Finding(
                            code="CP006",
                            name="CURSOR_CYCLE",
                            message="A previously returned cursor was returned again.",
                            locations=[
                                Location(traversal=run_number, page=seen_cursors[next_cursor]),
                                here,
                            ],
                        )
                    )
                seen_cursors.setdefault(next_cursor, page.number)
            if next_cursor is None and expected_page != len(traversal.pages):
                errors.append(Issue(message="Trace continues after a terminal page"))
            cursor = next_cursor

        terminal = bool(traversal.pages) and cursor is None and not cycle
        if traversal.stop == "terminal" and not terminal:
            errors.append(
                Issue(message="Trace claims completion without reaching a terminal state")
            )
        elif traversal.stop == "cycle" and not cycle:
            errors.append(Issue(message="Trace claims a cursor cycle without evidence"))
        elif traversal.stop in {"budget", "error"}:
            errors.append(
                Issue(
                    message=f"Traversal did not complete ({traversal.stop})",
                    traversal=run_number,
                )
            )
        if traversal.stop == "terminal" and terminal:
            complete.append((run_number, stream))
            if trace.oracle is not None:
                expected = {identity_key(item) for item in trace.oracle}
                actual = {identity_key(item) for item in stream}
                missing = [item for item in trace.oracle if identity_key(item) not in actual]
                extra = [item for item in stream if identity_key(item) not in expected]
                if missing:
                    # Neighbours bound a missing interval only for an ordered oracle.
                    if trace.oracle_ordered:
                        group: list[Identity] = []
                        left: Location | None = None
                        for expected_id in trace.oracle:
                            right = seen_items.get(identity_key(expected_id))
                            if right is None:
                                group.append(expected_id)
                                continue
                            if group:
                                findings.append(
                                    Finding(
                                        code="CP003",
                                        name="MISSING_ITEMS",
                                        message="Oracle items are missing near these neighbours.",
                                        locations=([left] if left else []) + [right],
                                        item_ids=group[:20],
                                        count=len(group),
                                    )
                                )
                                group = []
                            left = right
                        if group:
                            findings.append(
                                Finding(
                                    code="CP003",
                                    name="MISSING_ITEMS",
                                    message="Trailing oracle items were not reachable.",
                                    locations=[left] if left else [],
                                    item_ids=group[:20],
                                    count=len(group),
                                )
                            )
                    else:
                        findings.append(
                            Finding(
                                code="CP003",
                                name="MISSING_ITEMS",
                                message="Oracle items were not reachable (unordered oracle).",
                                item_ids=missing[:20],
                                count=len(missing),
                            )
                        )
                if extra:
                    findings.append(
                        Finding(
                            code="CP010",
                            name="UNEXPECTED_ITEMS",
                            message="Items were absent from the oracle.",
                            locations=[seen_items[identity_key(item)] for item in extra[:20]],
                            item_ids=extra[:20],
                            count=len(extra),
                        )
                    )
                if trace.oracle_ordered and not missing and not extra and stream != trace.oracle:
                    findings.append(
                        Finding(
                            code="CP004",
                            name="ORACLE_ORDER_MISMATCH",
                            message="The observed sequence differs from the ordered oracle.",
                            locations=[Location(traversal=run_number, page=1)],
                        )
                    )

    if trace.consistency == "static" and complete:
        baseline_number, baseline = complete[0]
        baseline_keys = [identity_key(item) for item in baseline]
        for number, stream in complete[1:]:
            if [identity_key(item) for item in stream] != baseline_keys:
                findings.append(
                    Finding(
                        code="CP009",
                        name="INCONSISTENT_TRAVERSAL",
                        message="Complete identity streams differ across limits or repetitions.",
                        locations=[
                            Location(traversal=baseline_number, page=1),
                            Location(traversal=number, page=1),
                        ],
                        possible_cause=(
                            "Non-unique ordering or a dataset that changed between traversals"
                        ),
                    )
                )
    for binding in trace.bindings:
        if binding.accepted_rejection:
            if not 400 <= binding.status < 500:
                errors.append(Issue(message="Invalid binding rejection evidence in trace"))
        elif 200 <= binding.status < 300:
            findings.append(
                Finding(
                    code="CP007",
                    name="CURSOR_BINDING_VIOLATION",
                    message=f"Cursor was accepted after changing parameter {binding.parameter}.",
                    requests=[binding.request],
                )
            )
        else:
            errors.append(Issue(message="Binding probe returned an unexpected HTTP status"))
    pages = sum(len(run.pages) for run in trace.traversals)
    return Report(
        outcome="error" if errors else "fail" if findings else "pass",
        summary=Summary(
            traversals=len(trace.traversals),
            pages=pages,
            responses=pages + len(trace.bindings),
            items=total_items,
            unique_items=len(all_ids),
        ),
        findings=findings,
        errors=errors,
        notes=trace.notes,
        trace=trace,
    )
