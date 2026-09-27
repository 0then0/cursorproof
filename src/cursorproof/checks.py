from cursorproof.config import Identity
from cursorproof.models import (
    BindingObservation,
    Finding,
    Issue,
    Location,
    Report,
    Summary,
    Trace,
)

type CursorPageState = tuple[
    str,
    tuple[tuple[type[str] | type[int], Identity], ...],
]


def identity_key(value: Identity) -> tuple[type[str] | type[int], Identity]:
    return type(value), value


def cursor_page_state(next_cursor: str, item_ids: list[Identity]) -> CursorPageState:
    return next_cursor, tuple(identity_key(item_id) for item_id in item_ids)


def analyze(trace: Trace) -> Report:
    findings: list[Finding] = []
    errors = list(trace.errors)
    if trace.consistency == "snapshot":
        if trace.oracle is None or trace.oracle_snapshot is None:
            errors.append(Issue(message="Snapshot trace lacks its initial full-record oracle"))
        elif not trace.oracle_ordered:
            errors.append(Issue(message="Snapshot trace requires an ordered oracle"))
        elif [identity_key(row.id) for row in trace.oracle_snapshot] != [
            identity_key(value) for value in trace.oracle
        ]:
            errors.append(Issue(message="Snapshot fingerprints do not match the oracle identities"))
        if any(
            item.fingerprint is None
            for run in trace.traversals
            for page in run.pages
            for item in page.items
        ):
            errors.append(Issue(message="Snapshot trace is missing a full-record fingerprint"))
    complete: list[tuple[int, list[Identity]]] = []
    all_ids: set[tuple[type[str] | type[int], Identity]] = set()
    total_items = 0
    for run_number, traversal in enumerate(trace.traversals, 1):
        seen_items: dict[tuple[type[str] | type[int], Identity], Location] = {}
        seen_sort_keys: dict[tuple[type[str] | type[int], Identity], list[int] | None] = {}
        seen_page_states: dict[CursorPageState, int] = {}
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
                    seen_sort_keys[key] = item.sort_key
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
                page_state = cursor_page_state(next_cursor, [item.id for item in page.items])
                previous_page = seen_page_states.get(page_state)
                if previous_page is not None and next_cursor == page.cursor:
                    cycle = True
                    findings.append(
                        Finding(
                            code="CP001",
                            name="CURSOR_NOT_ADVANCING",
                            message=(
                                "The same next cursor and page identities repeated "
                                "without adding new identities."
                            ),
                            locations=[here],
                        )
                    )
                elif previous_page is not None:
                    cycle = True
                    findings.append(
                        Finding(
                            code="CP006",
                            name="CURSOR_CYCLE",
                            message=(
                                "A next cursor and page identity sequence repeated "
                                "without adding new identities."
                            ),
                            locations=[
                                Location(traversal=run_number, page=previous_page),
                                here,
                            ],
                        )
                    )
                else:
                    seen_page_states.setdefault(page_state, page.number)
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
                if len(expected) != len(trace.oracle):
                    errors.append(Issue(message="Trace oracle contains duplicate identities"))
                    continue
                actual = {identity_key(item) for item in stream}
                missing = [item for item in trace.oracle if identity_key(item) not in actual]
                extra = [item for item in stream if identity_key(item) not in expected]
                expected_common = [
                    identity for identity in trace.oracle if identity_key(identity) in actual
                ]
                common_seen: set[tuple[type[str] | type[int], Identity]] = set()
                actual_common: list[Identity] = []
                for identity in stream:
                    key = identity_key(identity)
                    if key in expected and key not in common_seen:
                        actual_common.append(identity)
                        common_seen.add(key)
                order_mismatch = trace.oracle_ordered and expected_common != actual_common
                if trace.oracle_snapshot is not None:
                    expected_fingerprints = {
                        identity_key(item.id): item.fingerprint for item in trace.oracle_snapshot
                    }
                    content_changes: dict[
                        tuple[type[str] | type[int], Identity], tuple[Identity, Location]
                    ] = {}
                    for page in traversal.pages:
                        for position, item in enumerate(page.items, 1):
                            expected_fingerprint = expected_fingerprints.get(identity_key(item.id))
                            if (
                                expected_fingerprint is not None
                                and item.fingerprint is not None
                                and item.fingerprint != expected_fingerprint
                            ):
                                content_changes.setdefault(
                                    identity_key(item.id),
                                    (
                                        item.id,
                                        Location(
                                            traversal=run_number,
                                            page=page.number,
                                            position=position,
                                        ),
                                    ),
                                )
                    if content_changes:
                        findings.append(
                            Finding(
                                code="CP011",
                                name="SNAPSHOT_CONTENT_CHANGED",
                                message=(
                                    "Snapshot fields changed after reading the initial oracle."
                                ),
                                locations=[location for _, location in content_changes.values()],
                                item_ids=[item_id for item_id, _ in content_changes.values()][:20],
                                count=len(content_changes),
                            )
                        )
                if missing:
                    # Neighbours bound a missing interval only for an ordered oracle.
                    if trace.oracle_ordered and not order_mismatch:
                        group: list[Identity] = []
                        left: Location | None = None
                        left_key: tuple[type[str] | type[int], Identity] | None = None
                        for expected_id in trace.oracle:
                            expected_key = identity_key(expected_id)
                            right = seen_items.get(expected_key)
                            if right is None:
                                group.append(expected_id)
                                continue
                            if group:
                                right_sort_key = seen_sort_keys.get(expected_key)
                                left_sort_key = seen_sort_keys.get(left_key) if left_key else None
                                tie_possible = bool(
                                    trace.ordering_fields
                                    and left_sort_key
                                    and right_sort_key
                                    and left_sort_key[0] == right_sort_key[0]
                                )
                                findings.append(
                                    Finding(
                                        code="CP003",
                                        name="MISSING_ITEMS",
                                        message="Oracle items are missing near these neighbours.",
                                        locations=([left] if left else []) + [right],
                                        item_ids=group[:20],
                                        count=len(group),
                                        possible_cause=(
                                            "Non-unique ordering around the missing-item boundary"
                                            if tie_possible
                                            else None
                                        ),
                                    )
                                )
                                group = []
                            left = right
                            left_key = expected_key
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
                    elif order_mismatch:
                        findings.append(
                            Finding(
                                code="CP003",
                                name="MISSING_ITEMS",
                                message=(
                                    "Oracle items are missing; the page boundary is ambiguous "
                                    "because item order also differs."
                                ),
                                item_ids=missing[:20],
                                count=len(missing),
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
                if order_mismatch:
                    first_difference = next(
                        index
                        for index, (expected_id, actual_id) in enumerate(
                            zip(expected_common, actual_common, strict=True)
                        )
                        if identity_key(expected_id) != identity_key(actual_id)
                    )
                    actual_id = actual_common[first_difference]
                    expected_id = expected_common[first_difference]
                    findings.append(
                        Finding(
                            code="CP004",
                            name="ORACLE_ORDER_MISMATCH",
                            message="The relative order of oracle items differs from the oracle.",
                            locations=[
                                seen_items[identity_key(actual_id)],
                                seen_items[identity_key(expected_id)],
                            ],
                            item_ids=[actual_id, expected_id],
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
    if trace.expected_unique_items is not None and len(all_ids) != trace.expected_unique_items:
        findings.append(
            Finding(
                code="CP012",
                name="BOUNDARY_CARDINALITY_MISMATCH",
                message=(
                    f"Expected {trace.expected_unique_items} unique items, observed {len(all_ids)}."
                ),
            )
        )
    bindings: dict[int, list[BindingObservation]] = {}
    for binding in trace.bindings:
        bindings.setdefault(binding.case, []).append(binding)
    expected_binding_cases = set(range(1, trace.binding_cases_expected + 1))
    if set(bindings) != expected_binding_cases:
        errors.append(Issue(message="Trace does not contain all configured binding cases"))
    for observations in bindings.values():
        baselines = [item for item in observations if item.phase == "baseline"]
        cursor_probes = [item for item in observations if item.phase == "cursor"]
        if len(baselines) != 1 or len(cursor_probes) > 1:
            errors.append(Issue(message="Binding probe trace is incomplete or inconsistent"))
            continue
        baseline_observation = baselines[0]
        if not 200 <= baseline_observation.status < 300:
            errors.append(Issue(message="Changed query is invalid without a cursor"))
            continue
        if not baseline_observation.valid:
            errors.append(Issue(message="Changed query returned an invalid pagination response"))
            continue
        if len(cursor_probes) != 1:
            errors.append(Issue(message="Binding probe lacks its cursor request"))
            continue
        cursor_probe = cursor_probes[0]
        if cursor_probe.cursor is None:
            errors.append(Issue(message="Binding cursor evidence is missing"))
        elif (
            cursor_probe.status in trace.binding_reject_statuses
            and 400 <= cursor_probe.status < 500
        ):
            continue
        elif 200 <= cursor_probe.status < 300:
            findings.append(
                Finding(
                    code="CP007",
                    name="CURSOR_BINDING_VIOLATION",
                    message=(
                        f"Cursor was accepted after changing parameter {cursor_probe.parameter}."
                    ),
                    requests=[baseline_observation.request, cursor_probe.request],
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
