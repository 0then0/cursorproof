# Qdrant Scroll validation

This lab runs CursorProof's [POST JSON body mode](guide.md#post-pagination-with-a-json-body)
against a real, disposable [Qdrant](https://qdrant.tech/) server. It is an integration
check of the request and cursor handling. It is not a reproduction of a Qdrant defect,
and it makes no claim about other Qdrant versions, clusters or collections.

## What Scroll does

[Scroll points](https://api.qdrant.tech/v-1-20-x/api-reference/points/scroll-points) is
`POST /collections/{name}/points/scroll`. The JSON body takes `limit`, `offset` (a point
ID: an unsigned 64-bit integer or a UUID string), `filter`, `order_by`, `with_payload`
and `with_vector`. The response has `result.points` and `result.next_page_offset`, the
ID of the next page's first point (the lab's duplicate-free streams confirm that `offset` is inclusive). Without `order_by` points come in ID order. The
configuration maps these directly:

```yaml
method: POST
pagination: { cursor_param: offset, limit_param: limit, terminal_values: [null] }
response: { items: $.result.points, next_cursor: $.result.next_page_offset, id: $.id }
```

The last page returns `next_page_offset: null`, which the lab confirms against the pinned
version. CursorProof treats the cursor as opaque and returns exactly the value it received.

## Pinned environment

- Image `qdrant/qdrant:v1.20.0`, server version `1.20.0`, commit
  `222b2e44ab1c3f17e8246396ead6558e45041df7`, digest
  `sha256:9bfc89f9914fc14876a88c05270685921a55b8877b254a98bcc1cc2f0a424cd2`.
- A single container bound to `127.0.0.1:16333`, started with `docker run -d --rm` and
  removed when the script ends, even after a failure. Collections live inside it, so
  nothing else is created or modified.

Evidence is in [labs/qdrant-scroll/evidence](../labs/qdrant-scroll/evidence): `summary.json`
(versions, commands and results) and one trace per scenario. The exact configurations are
in [labs/qdrant-scroll/configs](../labs/qdrant-scroll/configs).

## Data and oracle

Two new collections, each with 44 points, 4-dimensional `Dot` vectors and a keyword index
on `group`. Every third point has `group: drop`; the rest have `group: keep`. The filter
`group == keep` therefore leaves 30 points.

- `cursorproof_ints`: IDs `0..39` plus `2^53+1`, `2^53+2`, `2^63` and `2^64-2`. The large
  values lose precision in a binary double, so they detect numeric coercion.
- `cursorproof_uuids`: 44 UUIDs derived from fixed names.

The expected stream comes from [fixture.py](../labs/qdrant-scroll/fixture.py), by
construction and not by querying Qdrant. [oracle.py](../labs/qdrant-scroll/oracle.py)
prints it as a JSON array for CursorProof's command oracle. UUID order is the unsigned
128-bit value, which the lab confirms matches Qdrant.

## Run it

Docker is required. The script uses the installed `cursorproof` and PyYAML:

```sh
uv run python labs/qdrant-scroll/run_lab.py
```

The first run pulls the pinned image. The script then creates the collections, runs
CursorProof with page sizes `[1, 7, 10, 25, 100]`, replays each trace offline, live-replays
traversal 2 (with `--execute-hooks`, because the oracle is a command) and rewrites the
configurations and evidence. Use `--port` or `--output` to change the port or output folder.

## Result

| Scenario | Exit | Result |
| --- | --- | --- |
| `ints` | 0 | All 5 traversals returned exactly the 30 expected IDs in order (41 pages in total) |
| `uuids` | 0 | Same, with UUID string cursors |
| `ints-without-filter` | 1 | Control: the filter is removed from the body; CP010 reports the 14 unfiltered points |

Offline replay and live replay give the same exit code as the original run for each scenario.
The control shows that the fixed filter really is part of every request.

## Limits

- Scroll's `next_page_offset` is the first ID of the next page, so Qdrant never returns
  the cursor `0` after a non-empty page. Point `0` is checked as an item identity; the
  cursor `0` is covered by the [local test API](../examples/scroll_api.py) and unit tests.
- Static data only: the lab does not write while scrolling, and uses one node and one shard.
- `order_by` pagination uses a different cursor shape and is not tested here.
- Replay of a live traversal needs the same pinned image and collections.
