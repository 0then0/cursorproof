# Django REST framework example

This example checks a real Django REST framework `CursorPagination` endpoint
with CursorProof. It demonstrates a repeated page when a large group of records
shares the first sort value, and compares that result with unique ordering.

The example uses Django **6.1.2**, Django REST framework **3.18.3**, a disposable
SQLite database and a local HTTP server. Framework versions are pinned in the
optional `integration` dependency group and `uv.lock`; installing CursorProof
alone does not install Django or DRF.

For your own API, start with [examples/drf.yml](../examples/drf.yml). Set DRF's
`page_size_query_param = "page_size"`, use `pagination.mode: url`, and extract
`response.next_cursor: $.next`. See the [continuation URL contract](guide.md#continuation-urls)
for the supported links and scope restrictions.

## Run the example

This example requires Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).
CursorProof itself supports Python 3.10 or newer; the integration environment
requires Python 3.12 because it includes Django 6.1.2. Clone the
repository and run the following commands from its root. They build CursorProof
and install the wheel into a separate integration environment, leaving the
checkout's `.venv` available for ordinary development:

```sh
uv build --out-dir /tmp/cursorproof-build
UV_PROJECT_ENVIRONMENT=/tmp/cursorproof-drf-env \
  uv sync --locked --group integration --no-install-project --python 3.12
uv pip install --python /tmp/cursorproof-drf-env/bin/python --no-deps \
  /tmp/cursorproof-build/cursorproof-0.2.0-py3-none-any.whl
/tmp/cursorproof-drf-env/bin/python examples/drf_regression.py \
  --output /tmp/cursorproof-drf-evidence
```

The script starts an HTTP server on an available loopback port, populates a
single temporary SQLite database through Django's ORM, and invokes the installed
CursorProof CLI. The endpoint returns DRF's standard `next`, `previous` and
`results` fields. The script uses the installed `CursorPagination` without
replacing its methods or transforming its responses.

The output directory contains a configuration and sanitized trace for each
case, plus `summary.json`. The server stops and the database is removed after
execution, including when an assertion fails. The output files remain available
for inspection and offline replay. Rerunning with the same output directory
replaces files with matching names.

## Dataset and controls

The database contains 2500 unchanged records with integer IDs `1..2500` and
`active = 1`. The `active` field is an integer so it can be checked with
CursorProof's numeric ordering type.

- `/ties/` uses `ordering = ("active", "id")`.
- `/unique/` uses `ordering = ("id",)` and provides the control.
- `group=all` selects all 2500 records.
- `group=small` selects IDs `1..800` from the same database, below the offset
  cutoff.

The paginator inherits DRF's `offset_cutoff = 1000`. The script verifies that
value and configures only standard page size and ordering attributes.

Each of the three cases runs at page sizes 100, 250 and 500, with one traversal
per run, `max_pages = 40` and `max_items = 5000`. Data does not change between
runs.

The independent `/oracle/` endpoint reads all IDs directly from the ORM without
pagination. API and oracle apply the same `group` filter. The oracle orders by
ID, which matches both API orderings because every `active` value is equal.
CursorProof does not decode DRF cursors to determine expected records.

## Expected results

The checked-in [evidence summary](drf-evidence.json), recorded on Python 3.13.15,
contains versions, dataset parameters, page ranges, cursor aliases, findings,
stop reasons, exit codes and replay comparisons for all nine cases.

For the large tie group:

- Page size 100: 12 pages, 1200 observations and 1100 distinct IDs.
  One CP001, 100 CP002 findings and one CP004.
- Page size 250: 6 pages, 1500 observations and 1250 distinct IDs.
  One CP001, 250 CP002 findings and one CP004.
- Page size 500: 4 pages, 2000 observations and 1500 distinct IDs.
  One CP001, 500 CP002 findings and one CP004. The page ranges are
  `1..500`, `501..1000`, `1001..1500`, `1001..1500`.

All three stop with `stop=cycle`, remain incomplete and return exit **1**.
CP001 reports a cursor that does not advance, CP002 reports duplicate IDs, and
CP004 reports the backward ordering jump at the repeated page. No CP003 missing
item finding is produced: the traversal did not reach normal termination, so
CursorProof cannot establish which expected records would be absent from a
complete stream.

The unique-ordering control returns all 2500 IDs in exact oracle order without
findings, using 25, 10 and 5 pages respectively. The small tie group returns all
800 IDs without findings, using 8, 4 and 2 pages. These six runs stop with
`stop=terminal` and return exit **0**.

The script asserts that the large tie group produces findings and remains
incomplete. It also asserts exact oracle equality for the controls, full offline
report equality, and matching observations during live replay. The finding
counts above describe the saved evidence; the integration test does not require
that exact list for detection to succeed.

## Replay the evidence

From a checkout installed with `uv sync --locked`, replay the two representative
traces without Django, the database or a running API:

```sh
uv run --no-sync cursorproof replay docs/drf-problematic-500-trace.json
uv run --no-sync cursorproof replay docs/drf-control-500-trace.json
```

The first command returns exit **1** with a cycle and CP001/CP002/CP004 findings.
The second returns exit **0** with normal termination and exact oracle equality.
These different exit codes are expected.

After running the example, replay any generated trace with the integration
interpreter:

```sh
/tmp/cursorproof-drf-env/bin/python -m cursorproof replay \
  /tmp/cursorproof-drf-evidence/problematic-500-trace.json --format json
/tmp/cursorproof-drf-env/bin/python -m cursorproof replay \
  /tmp/cursorproof-drf-evidence/control-500-trace.json --format json
```

Generated configurations point to the temporary server's port. Live replay is
checked while the script is running; after the server stops, use offline replay
or run the example again. Traces contain cursor aliases and sanitized request
URLs. `summary.json` and `docs/drf-evidence.json` summarize the results and are
not replay traces.

## Why the tie group fails

DRF documents that cursor ordering should be unique or nearly unique. Its cursor
position uses the first ordering field, while an offset distinguishes records
with the same value. A group larger than the offset cutoff can repeat a page.
Adding `id` as a second ordering field does not make this distribution pass;
using the unique ID as the first field does in this example.

This demonstrates an application pagination contract that does not hold for its
data distribution, rather than a newly discovered DRF defect. The scenario is
based on [discussion #9504](https://github.com/encode/django-rest-framework/discussions/9504),
[discussion #7888](https://github.com/encode/django-rest-framework/discussions/7888)
and the [DRF pagination guide](https://www.django-rest-framework.org/api-guide/pagination/#cursorpagination).

The saved results cover the pinned framework versions, SQLite, static data and
these page sizes. They do not establish behavior for other databases, framework
versions, reverse traversal or concurrent changes. Choose ordering appropriate
to your API and test its actual data distribution.

## Integration tests

With the wheel installed as described above, run:

```sh
/tmp/cursorproof-drf-env/bin/python -m pytest tests/test_drf_integration.py
```

The tests run all nine HTTP/ORM cases and verify resource cleanup after a failed
check. They are skipped when Django or DRF is absent. URL-mode unit tests live
in `tests/test_url_pagination.py` and use HTTPX transports without Django.

CI builds and installs the wheel, then runs the integration tests on Python
3.12 through 3.14. Core checks also run on Python 3.10 and 3.11.
To run the remaining project checks, see
[Development](../README.md#development).
