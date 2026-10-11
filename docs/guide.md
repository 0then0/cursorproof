# User guide

Use CursorProof to traverse a REST API, check its pagination contract, and replay
the recorded observations. For installation and a local demo, see the
[README](../README.md).

- [Run a check](#run-a-check)
- [Configure the API and response fields](#configuration)
- [Follow continuation URLs](#continuation-urls)
- [Compare against an oracle](#oracle)
- [Choose consistency and mutation checks](#consistency-and-mutation-hooks)
- [Test cursor binding](#cursor-binding)
- [Read findings and exit codes](#findings-and-exit-codes)
- [Save and replay traces](#reproduction-and-privacy)

## Run a check

Save your YAML or JSON configuration, then validate it before making requests:

```sh
cursorproof check cursorproof.yml
cursorproof run cursorproof.yml
```

`check` validates fields and environment references without sending HTTP requests
or executing commands. `run` traverses the endpoint once for each configured
page size and repetition, displays findings, and saves `cursorproof-repro.json`.

For JSON output and a named trace:

```sh
cursorproof run cursorproof.yml --format json --repro orders-trace.json
cursorproof replay orders-trace.json --format json
```

Offline replay needs only the trace. It does not contact the API or execute
commands. Use `cursorproof --help` and `cursorproof COMMAND --help` for all options.

## Configuration

```yaml
url: http://localhost:8000/api/orders
headers:
  Authorization: Bearer ${API_TOKEN}
parameters:
  status: open
pagination:
  mode: token # default; url follows explicit continuation links
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

### Response fields and ordering

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
Datetime ordering requires ISO 8601 with a timezone. UTC `Z`, explicit offsets,
fractional seconds, compact dates/times and ISO week dates are supported on all
supported Python versions. Fractions longer than six digits are truncated to
microseconds. String ordering uses Unicode
code point order, not a database locale/collation. `nulls` is absolute, independent
of ascending/descending direction.

### Defaults and request limits

Defaults: limits `[1, 10, 50, 51, 100]`, one repetition, static consistency, and the
resource limits shown above. Limits apply per traversal, response size per response.
HTTP timeout is HTTPX's timeout per network operation, not a total run deadline.
Requests are sequential; redirects are not followed, retries are not automatic.
URL query parameters are preserved; `parameters` overrides matching query values.

## Continuation URLs

`pagination.mode` is `token` by default. Token mode sends the extracted string as
the `cursor_param` query value, even if it looks like a URL. URL mode is available
in CursorProof 0.2.0 and later. Set `mode: url`
explicitly for an API that returns a link to the next page. Keep using
`response.next_cursor` to select that field, for example DRF's `next`:

```yaml
url: http://localhost:8000/items/
pagination:
  mode: url
  cursor_param: cursor
  limit_param: page_size
response:
  items: $.results
  next_cursor: $.next
  id: $.id
ordering:
  - field: id
    type: number
limits: [100, 250, 500]
```

For DRF, configure its `page_size_query_param = "page_size"`. The first request
uses the normal URL/query merge: `parameters` overrides matching URL query values
and the selected limit is added. Repeated URL query values for other names are
preserved. The first request defines the scope and endpoint for that traversal.

### Link validation

Each non-null continuation must satisfy this contract before another request:

- Supported references are absolute HTTP(S), scheme-relative (`//host/path`),
  root-relative (`/items/?...`), path-relative (`items?...`), and query-only
  (`?cursor=...&page_size=...`). Resolution uses the **current request URL**, with
  standard relative-reference rules. A path-relative reference must still resolve
  to the same endpoint.
- Scheme, case-insensitive host, and effective port must match the first request.
  Omitted ports mean 80 for HTTP and 443 for HTTPS. Host aliases are not treated
  as equal. The encoded path must match exactly after HTTPX URL preparation;
  trailing slashes and percent-encoded path variants are not interchangeable.
- Only query occurrences named by `cursor_param` may change, appear or disappear.
  Their values are opaque continuation state, including repeated values and
  percent-encoded bytes that are not UTF-8. CursorProof does not decode them to
  compare scope. Query names are decoded as UTF-8 to identify `cursor_param`.
  Every other name must preserve its ordered list of decoded UTF-8 values,
  including repeats and blank values. This protects filters, tenant selection, ordering, credentials
  in query parameters, and page size. Ordering between _different_ query names
  may change; ordering between values of the _same_ name may not. Addition or
  removal of any other parameter is an error.
- The selected page size must remain present exactly once with the same decimal
  string. A changed, omitted, padded, or repeated limit is rejected. CursorProof
  never inserts or overwrites parameters in a continuation URL. APIs with fixed
  page sizes that remove the limit parameter are outside this contract.
- References must be nonempty ASCII URIs. Whitespace, controls, backslashes,
  unescaped characters such as quotes, angle/curly brackets or pipes, malformed
  percent escapes, userinfo, fragments (including empty `#`), unsupported
  schemes, dot path segments, and encoded path separators are rejected. Encode
  non-ASCII scope names and values in UTF-8 percent escapes and use ASCII
  hostnames. Opaque cursor bytes need valid percent escapes but need not be UTF-8.

The continuation query is sent without decoding/re-encoding its cursor or merging
client parameters. HTTPX performs ordinary URL preparation, such as host casing
and default-port handling. Redirects are never followed. A disallowed target
receives no request, cookies or configured headers. Missing continuation fields,
invalid types, malformed links, and contract violations produce execution errors
(exit 2), not terminal pages or ordinary pagination findings.

### Termination and supported checks

JSON `null` is the only URL-mode terminal value. `terminal_values` must be `[null]`;
empty links are errors. Short and empty pages with valid links continue.
Duplicate, ordering, `has_more`, cycle and budget checks apply in both modes.

Continuation equality uses the resolved HTTPX URL string, without sorting the
opaque query or combining equivalent query encodings. Different URL text alone
does not establish item progress. Duplicate and ordering checks still apply,
and budgets limit traversal even when no identical page state repeats.
Cycle stops have `stop: cycle` and exit 1; budget and execution failures have
exit 2. None establishes completeness or produces missing-item comparisons
that require normal termination.

The consistency modes described below apply to both pagination modes.
URL mode rejects `cursor_binding` and `boundary_testing` during configuration
validation. Those checks support token mode only. Mutation hooks require one
limit and one repetition, as described below.

See the [Django REST framework example](drf-regression.md) for a local HTTP/ORM
setup, an independent oracle, pinned versions and replay commands.

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

### Oracle commands and identity types

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

### Static data

`static` is the default. All traversals must observe the same unchanged dataset;
complete ordered ID streams are compared across limits and repetitions. Set
`repeats: 2` or more to test repeated traversal at each limit.

### Snapshot consistency

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

### Live keyset traversal

`live-keyset` checks observed order and duplicate identities with immutable sort
keys and identities. It allows inserts/deletes and makes no completeness claim.
It requires `immutable_ordering: true` and configured ordering; an oracle is rejected
because a static list cannot describe general concurrent-write visibility.
Updating sort keys or reusing deleted identities is outside this model.

### Mutation hooks

Mutation modes require **one limit and one repetition**. Hooks execute
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
CursorProof does not reset datasets automatically. Supply fixture commands to
prepare and restore the data when using mutation hooks or boundary testing.

### Cardinality boundaries

For API cardinality boundaries, opt in to fixture commands:

```yaml
boundary_testing:
  setup: [python, fixtures.py, set-count, '{count}']
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

Cursor binding checks whether an API rejects a cursor when a query parameter
changes. It is available in token mode with static consistency. Declare
original query values and explicit alternatives. Each probe changes one
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

- `CP001 CURSOR_NOT_ADVANCING`: the same self-loop cursor and page identities
  repeat without adding new identities.
- `CP002 DUPLICATE_ITEM`: identity repeats within or between pages.
- `CP003 MISSING_ITEMS`: oracle identities are absent after a complete traversal.
- `CP004 ORDER_VIOLATION` / `ORACLE_ORDER_MISMATCH`: declared or oracle order differs.
- `CP005 PAGE_SIZE_EXCEEDED`: response contains more items than requested.
- `CP006 CURSOR_CYCLE`: the same next cursor and page identity sequence repeat
  without adding new identities.
- `CP007 CURSOR_BINDING_VIOLATION`: changed-query cursor was accepted.
- `CP008 TERMINATION_ERROR`: `has_more` contradicts terminal cursor semantics.
- `CP009 INCONSISTENT_TRAVERSAL`: complete streams differ across runs.
- `CP010 UNEXPECTED_ITEMS`: returned identities are absent from the oracle.
- `CP011 SNAPSHOT_CONTENT_CHANGED`: a selected record field differs from the initial oracle.
- `CP012 BOUNDARY_CARDINALITY_MISMATCH`: unique item count differs from the prepared fixture size.

### Interpreting findings

Findings contain locations and affected IDs where available. An ordered oracle
locates missing intervals between the nearest observed neighbours. These bound the
gap but do not prove which request caused it. An unordered oracle cannot locate
the gap. Cross-limit differences may suggest non-unique ordering, but do not
prove the underlying cause.

Exit codes: `0` all executed checks passed, `1` invariant violations, `2` invalid
configuration or incomplete execution. Errors take precedence over findings, but
both remain in the report. A passing result is limited to the observed scenarios
and configured contract, not proof for every possible dataset.

`--format json` writes a machine-readable report to stdout without progress text.
The summary counts observed items across all traversals and distinct identities
across those traversals. `responses` counts recorded page and binding responses;
it excludes oracle responses and failed requests.

Cursor values are opaque: repeating a token alone does not indicate a cycle. CursorProof
continues when a repeated token returns new identities. It reports a cycle when the same
next cursor and page identity sequence recur without new identities. `max_pages`
still bounds traversals that continue without reaching a terminal page or repeating an
observable page state.

## Reproduction and privacy

### Offline replay

`run` saves `cursorproof-repro.json` by default; use `--repro PATH` to change it.
The default trace filename is ignored by Git.
Existing trace files at that path are replaced atomically. `replay` rechecks the
recorded observations offline, without HTTP or commands:

```sh
cursorproof replay cursorproof-repro.json
```

### Live replay

To rerun one saved traversal against the API, provide the original configuration
and a traversal number:

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
Keep command credentials and dataset selection in separate environment variables.
Changing a dataset argument must remain visible to live replay, even when an
argument contains a secret. The CLI retains this distinction when it loads your
configuration. For Python callers, pass both the configuration and secrets
returned by `load_config` to `run`. A plain secret set that cannot classify a
secret-containing command argument disables live replay for the resulting trace;
offline replay remains available.
Ordinary query parameter values also affect the fingerprint, including values expanded
from environment variables, so changing a tenant or filter rejects live replay.

### Trace compatibility

Trace schema version 2 migrates version 1 static traces. Older snapshot and cursor-binding
traces replay as incomplete because they lack the evidence those checks now require.
Older version 2 traces without a replay fingerprint remain available for offline replay
but cannot be live-replayed safely.
Traces with the older configuration fingerprint algorithm remain available for offline
replay; generate a fresh trace for live replay with the current contract algorithm.
Live replay compares that contract, not the display URL, which may contain redactions.
Both token and URL mode use trace schema 2. URL-mode `cursor` and `next_cursor`
contain aliases for entire resolved continuation URLs. Request descriptions use
the same aliases for cursor query values and mask sensitive query parameters.
Raw continuation URLs are not stored in a separate field.

Token-mode replay fingerprints remain compatible when `mode: token` is explicit
or omitted. Switching between token and URL mode changes the replay contract
and rejects live replay with a configuration-contract mismatch.

### Recorded data and redaction

The trace stores page boundaries, identity sequences, status codes, request
descriptions, cursor aliases and order-preserving ranks for sort values. It
does not store full response bodies, headers, commands or raw cursors.

Environment substitutions and configured header values are redacted from recorded
strings; cursor aliases retain equality/cycle evidence. Snapshot field values are
stored as SHA-256 fingerprints, not as plain text. IDs containing known secrets
receive stable aliases within the run. Sensitive query names are masked. Ordinary
item IDs are retained for debugging, so traces may still contain application data.
Credentials should always come from environment references. Raw sort values are
omitted; ranks preserve ordering evidence, not the original values.

Offline replay reproduces checks on the saved evidence. Live replay needs the
original configuration, credentials and suitable current data. Restore the test
dataset before a live replay when necessary; CursorProof cannot restore expiring
cursors, external state or mutation effects automatically.
