# User guide

This guide covers configuration, checks, findings, and reproduction traces. For a quick installation and demo, see the [README](../README.md).

## Configuration

```yaml
url: http://localhost:8000/api/orders
headers:
  Authorization: Bearer ${API_TOKEN}
parameters:
  status: open
pagination:
  cursor_param: cursor
  limit_param: limit
  terminal_values: [null]
response:
  items: $.results
  next_cursor: $.next_cursor
  has_more: $.has_more # optional, but must be boolean when configured
  id: $.id
ordering:
  - field: created_at
    direction: desc
    type: datetime
    nulls: last
  - field: id
    direction: desc
    type: number
limits: [1, 2, 5, 10, 50, 51, 100]
repeats: 2
consistency: static
timeout: 10
max_pages: 10001
max_items: 100000
max_response_bytes: 10000000
```

JSON configuration is also supported (`.json`). Unknown fields are rejected.
Environment references are expanded **after parsing**, so quotes and newlines in
values cannot inject YAML. Missing or empty variables are errors. Configuration
validation does not send requests or execute commands.

Paths support `$`, dotted fields (`$.results`, `$.meta.cursor`), and array indices
(`$.data[0].id`); they are a small notation, not full JSONPath. Item paths are
relative to each item. Bare field names such as `created_at` are allowed.

IDs must be strings or integers; `"1"` and `1` are different identities. Cursor
values must be strings or explicit terminal values. An empty string is a valid
cursor unless added to `terminal_values`. A missing cursor field is an error.
An empty page with a continuation cursor is allowed. A short page alone does not
signal completion. Ordering is lexicographic across **all** configured fields,
within and between pages. `type` is `auto`, `string`, `number`, or `datetime`;
`auto` preserves JSON numbers/strings, and mixed incompatible types are errors.
Datetime ordering requires ISO 8601 with a timezone. String ordering uses Unicode
code point order, not a database locale/collation. `nulls` is absolute, independent
of ascending/descending direction.

Defaults: limits `[1, 10, 50, 51, 100]`, one repetition, static consistency, and the
resource limits shown above. Limits apply per traversal, response size per response.
HTTP timeout is HTTPX's timeout per network operation, not a total run deadline.
Requests are sequential; redirects are not followed, retries are not automatic.
URL query parameters are preserved; `parameters` overrides matching query values.

## Oracle

An oracle must contain exactly the same filtered/authorized population as the API.
It is fetched once before traversals. Static mode requires unchanged data throughout.

An HTTP oracle returning an array of IDs:

```yaml
oracle:
  url: http://localhost:8000/internal/orders/all
  headers:
    Authorization: Bearer ${ORACLE_TOKEN}
  items: $.ids
  id: $
  ordered: true
```

Or a command returning one string ID per line:

```yaml
oracle:
  command:
    - psql
    - ${DATABASE_URL}
    - -Atc
    - SELECT id FROM orders ORDER BY created_at DESC, id DESC
  format: lines
  ordered: true
```

Commands are argument arrays, executed without a shell, relative to the config's
directory. A command inherits the process environment. Command stderr is not
included in reports. Oracle commands and mutation hooks must wait for their work
to finish and must not detach child processes. A timeout may not stop a descendant
that starts a new session or redirects the supervisor's output streams. Use
`format: json` (default) to preserve integer IDs; a lines oracle always produces
string IDs. JSON oracles can use `items` and `id` paths.
Duplicate oracle IDs are an oracle error. Oracle credentials/cookies are isolated
from the pagination client. Set `ordered: false` for set equality, still checking
API duplicates separately.

Without an oracle, CursorProof checks internal consistency but **cannot establish
completeness**. Equal streams across limits can all omit the same records.

## Consistency and mutation hooks

`static` is the default. All traversals must observe the same unchanged dataset;
complete ordered ID streams are compared across limits and repetitions. Set
`repeats: 2` or more to test repeated traversal at each limit.

`snapshot` requires an ordered oracle and `response.snapshot_fields: [$]`. The oracle
must return full JSON records with the same shape as the API. CursorProof fingerprints
each complete record before traversal and detects membership, order, or content changes.
It cannot create a shared database/API snapshot through generic HTTP, so the test
environment must ensure the oracle represents the same snapshot, including the interval
before the first page.

```yaml
consistency: snapshot
response:
  items: $.results
  next_cursor: $.next_cursor
  id: $.id
  snapshot_fields: [$]
oracle:
  url: http://localhost:8000/internal/orders/all
  items: $.results
  id: $.id
```

`live-keyset` checks observed order and duplicate identities with immutable sort
keys and identities. It allows inserts/deletes and makes no completeness claim.
It requires `immutable_ordering: true` and configured ordering; an oracle is rejected
because a static list cannot describe general concurrent-write visibility.
Updating sort keys or reusing deleted identities is outside this model.

Mutation modes currently require **one limit and one repetition**. Hooks execute
between the specified page and the next page, in configuration order:

```yaml
consistency: snapshot
limits: [5]
response:
  snapshot_fields: [$]
oracle:
  url: http://localhost:8000/internal/orders/all
  items: $.results
  id: $.id
mutations:
  - after_page: 1
    command: [python, mutate.py, insert]
    timeout: 30
```

Hooks on a terminal page do not execute. An unreachable hook, command failure,
timeout, or traversal budget produces an incomplete/error result. Hooks execute
real commands: run these configurations against controlled test environments. A
timeout stops the command's process group; commands must not leave detached descendants.
This is command timeout handling, not process isolation or rollback. A detached process
can escape both its process group and parent PID ancestry before cleanup. Run commands
that cannot honor this contract inside an externally managed disposable container or
OS sandbox with a lifetime limit. CursorProof does not install or manage such isolation.
There is no built-in dataset reset adapter or scenario shrinking. The opt-in
boundary command below uses fixture commands supplied by the API owner.
Hypothesis is used to test CursorProof's own invariants and boundary cases.

For API cardinality boundaries, opt in to fixture commands:

```yaml
boundary_testing:
  setup: [python, fixtures.py, set-count, "{count}"]
  cleanup: [python, fixtures.py, reset]
  timeout: 30
```

Run `cursorproof boundary cursorproof.yml`. For each configured limit, it prepares
0, 1, `limit - 1`, `limit`, `limit + 1`, `2 * limit`, and `2 * limit + 1` records,
deduplicating repeated sizes. The `{count}` placeholder is replaced with the target
size. Each case gets a fresh traversal and oracle read; cleanup runs after every case.
The fixture commands must create exactly the requested population and restore a safe
state when cleanup runs. Boundary tests execute real commands and can change data.
The first failing scenario is saved to `cursorproof-boundary-repro.json` by default;
use `--repro PATH` to choose another file. Replay it offline with
`cursorproof replay PATH`. Live replay with `--config` also requires
`--execute-hooks`; it prepares the saved fixture size, reruns the traversal, and then
cleans up.

## Cursor binding

Declare original query values and explicit alternatives. Each probe changes one
parameter while keeping the others and reusing a cursor from the original stream:

```yaml
parameters:
  status: open
  sort: -created_at
cursor_binding:
  parameters:
    status: [closed]
    sort: [created_at]
  mismatch_behavior: reject
  reject_statuses: [400, 409, 422]
```

Binding probes run once using the first usable continuation cursor in static mode.
A 2xx response violates the declared rejection policy. Other unlisted statuses
(including 401, 403, 429, and 5xx by default) are inconclusive errors, not evidence of
correct binding. No available cursor makes this requested check incomplete.
This tests query parameters, not a change of authentication identity or headers.
Before sending a cursor probe, CursorProof sends the changed query without a cursor;
that baseline must succeed. This prevents an invalid alternative value from falsely
proving that the cursor is bound.

## Findings and exit codes

- `CP001 CURSOR_NOT_ADVANCING`: next cursor equals the request cursor.
- `CP002 DUPLICATE_ITEM`: identity repeats within or between pages.
- `CP003 MISSING_ITEMS`: oracle identities are absent after a complete traversal.
- `CP004 ORDER_VIOLATION` / `ORACLE_ORDER_MISMATCH`: declared or oracle order differs.
- `CP005 PAGE_SIZE_EXCEEDED`: response contains more items than requested.
- `CP006 CURSOR_CYCLE`: a longer cursor cycle is detected.
- `CP007 CURSOR_BINDING_VIOLATION`: changed-query cursor was accepted.
- `CP008 TERMINATION_ERROR`: `has_more` contradicts terminal cursor semantics.
- `CP009 INCONSISTENT_TRAVERSAL`: complete streams differ across runs.
- `CP010 UNEXPECTED_ITEMS`: returned identities are absent from the oracle.
- `CP011 SNAPSHOT_CONTENT_CHANGED`: a selected record field differs from the initial oracle.
- `CP012 BOUNDARY_CARDINALITY_MISMATCH`: unique item count differs from the prepared fixture size.

Findings contain locations and affected IDs where available. An ordered oracle
locates missing intervals between the nearest observed neighbours. These bound the
gap but do not prove which request caused it. An unordered oracle cannot locate
the gap. Cross-limit differences
may suggest non-unique ordering, but do not prove the underlying cause.

Exit codes: `0` all executed checks passed, `1` invariant violations, `2` invalid
configuration or incomplete execution. Errors take precedence over findings, but
both remain in the report. A passing result is limited to the observed scenarios
and configured contract, not proof for every possible dataset.

`--format json` writes a machine-readable report to stdout without progress text.
The summary counts observed items across all traversals and distinct identities
across those traversals. `responses` counts recorded page and binding responses;
it excludes oracle responses and failed requests.

## Reproduction and privacy

`run` saves `cursorproof-repro.json` by default; use `--repro PATH` to change it.
The default trace filename is ignored by Git.
Existing trace files at that path are replaced atomically. `replay` rechecks the
recorded observations offline, without HTTP or commands. To reissue one saved traversal
against the API, provide the original configuration and a traversal number:

```sh
cursorproof replay cursorproof-repro.json --config cursorproof.yml --traversal 2
```

Live replay runs the selected limit once, without the other limits and repetitions.
If the config contains mutation hooks, pass `--execute-hooks` to run those commands.
They can change real data, so restore the test environment before replay.
The trace includes a fingerprint of the sanitized replay configuration. Live replay
rejects changes to response paths, ordering, oracle, mutation hooks, and other checks.
Sensitive authentication header values are excluded from the fingerprint so credentials
can rotate; other header values remain part of the replay contract.
Because rotated credentials can represent a different principal or tenant, keep the
authorization scope stable and express tenant or dataset selection in a non-secret
header or query parameter so live replay can compare it.
Live replay detects credential header and query names in the API and HTTP oracle
configuration and refuses to run unless you pass `--allow-unverified-auth-scope`.
Use this flag only after independently confirming the same principal and scope.
The flag records that scope was not verified; it does not validate token identity.
Custom authentication schemes with unrecognized names cannot be detected automatically.
For file-loaded configurations, command fingerprints preserve sensitive environment substitutions,
so rotating a PIN cannot hide a change to an unrelated dataset argument. Keep dataset
selection in a separate, non-secret variable rather than embedding it in credentials.
Programmatic callers should pass both values returned by `load_config` to `run`.
When a plain secret set is used and a secret remains in an unclassified command value,
the trace has no live-replay fingerprint; offline replay remains available. This avoids
both hiding unrelated command data and exposing a guessable hash of a short secret.
Ordinary query parameter values also affect the fingerprint, including values expanded
from environment variables, so changing a tenant or filter rejects live replay.
Trace schema version 2 migrates version 1 static traces. Older snapshot and cursor-binding
traces replay as incomplete because they lack the evidence those checks now require.
Older version 2 traces without a replay fingerprint remain available for offline replay
but cannot be live-replayed safely.
Traces with the older configuration fingerprint algorithm remain available for offline
replay; generate a fresh trace for live replay with the current contract algorithm.
Live replay compares that contract, not the display URL, which may contain redactions.
The versioned trace stores page boundaries, identity sequences, status codes,
request descriptions, cursor aliases, and order-preserving ranks for sort values.
It does not store full response bodies, headers, commands, or raw cursors.

Environment substitutions and configured header values are redacted from recorded
strings; cursor aliases retain equality/cycle evidence. Snapshot field values are
stored as SHA-256 fingerprints, not as plain text. IDs containing known secrets
receive stable aliases within the run. Sensitive query names are masked. Ordinary
item IDs are retained for debugging, so traces may still contain application data.
Credentials should always come from environment references. Raw sort values are
omitted; ranks preserve ordering evidence, not the original values.

Offline replay reproduces the checks on recorded evidence. Live replay needs the
original configuration, credentials, and suitable current data; expiring cursors,
external state, and mutation effects cannot be restored automatically.

