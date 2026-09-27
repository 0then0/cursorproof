from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from cursorproof.checks import analyze
from cursorproof.models import Item, Page, Trace, Traversal


class PaginationMachine(RuleBasedStateMachine):
    """Generate traversals including repeated identities and cursor transitions."""

    def __init__(self) -> None:
        super().__init__()
        self.pages: list[Page] = []
        self.stream: list[int] = []
        self.cycle: str | None = None

    @precondition(lambda self: self.cycle is None)
    @rule(items=st.lists(st.integers(0, 10), max_size=4))
    def fetch_page(self, items: list[int]) -> None:
        self.pages.append(
            Page(
                number=len(self.pages) + 1,
                request="https://api.test/orders",
                cursor=self.pages[-1].next_cursor if self.pages else None,
                next_cursor=f"cursor_{len(self.pages) + 1}",
                items=[Item(id=item) for item in items],
            )
        )
        self.stream.extend(items)

    @precondition(lambda self: len(self.pages) >= 2 and self.cycle is None)
    @rule(immediate=st.booleans())
    def loop_cursor(self, immediate: bool) -> None:
        cursor = self.pages[-1].next_cursor
        if immediate:
            transitions = [(cursor, cursor), (cursor, cursor)]
            self.cycle = "CP001"
        else:
            next_cursor = f"loop_{len(self.pages)}"
            transitions = [
                (cursor, next_cursor),
                (next_cursor, cursor),
                (cursor, next_cursor),
            ]
            self.cycle = "CP006"
        for request_cursor, response_cursor in transitions:
            self.pages.append(
                Page(
                    number=len(self.pages) + 1,
                    request="https://api.test/orders",
                    cursor=request_cursor,
                    next_cursor=response_cursor,
                    items=[],
                )
            )

    @rule()
    def observe(self) -> None:
        pass

    @invariant()
    def findings_match_observed_contract(self) -> None:
        pages = self.pages.copy()
        if self.cycle is None:
            pages.append(
                Page(
                    number=len(pages) + 1,
                    request="https://api.test/orders",
                    cursor=pages[-1].next_cursor if pages else None,
                    next_cursor=None,
                    items=[],
                )
            )
        trace = Trace(
            tool_version="test",
            consistency="static",
            traversals=[
                Traversal(limit=4, pages=pages, stop="cycle" if self.cycle else "terminal")
            ],
        )
        report = analyze(trace)
        codes = {finding.code for finding in report.findings}
        assert ("CP002" in codes) == (len(self.stream) != len(set(self.stream)))
        assert not report.errors
        if self.cycle:
            assert self.cycle in codes
        else:
            assert not {"CP001", "CP006"} & codes
        assert analyze(Trace.model_validate_json(trace.model_dump_json())) == report


TestPaginationMachine = PaginationMachine.TestCase
TestPaginationMachine.settings = settings(max_examples=25, stateful_step_count=15, deadline=None)
