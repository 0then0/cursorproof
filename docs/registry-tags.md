# Distribution Registry tags validation

This lab runs CursorProof's [Link header continuation](guide.md#link-header-continuation)
against a real, disposable [Distribution Registry](https://distribution.github.io/distribution/).
It is an integration check of Link parsing, URL validation and termination. It is not a
reproduction of a registry defect, and it makes no claim about other registry versions,
storage drivers or authenticated registries.

## What the API does

[Listing image tags](https://distribution.github.io/distribution/spec/api/#listing-image-tags)
is `GET /v2/<name>/tags/list?n=<integer>&last=<tag>`. The JSON body is
`{"name": ..., "tags": [...]}`, ordered lexically, with entries after `last`. The body has no
continuation field. When more entries exist, the response carries

```http
Link: </v2/cursorproof/tags/tags/list?last=10&n=5>; rel="next"
```

and the specification says a client should use this value to proceed and stop when the
header is no longer set. The configuration maps this directly:

```yaml
pagination: { mode: url, continuation: link-header, cursor_param: last, limit_param: n }
response: { items: $.tags, id: $ }
```

## Pinned environment

- Image `registry:3.0.0`, digest
  `sha256:6c5666b861f3505b116bb9aa9b25175e71210414bd010d92035ff64018f9457e`, reporting
  `registry github.com/distribution/distribution/v3 3.0.0`. The script runs the image by
  digest.
- A single container bound to `127.0.0.1:15000`, started with `docker run -d --rm` and
  `--tmpfs /var/lib/registry`, so the data lives in memory and no volume is created. The
  container is removed when the script ends, even after a failure, and the summary records
  that it is gone. The pulled image stays in the local Docker image store.
- No existing registry is used and nothing is pushed anywhere else.

Evidence is in [labs/registry-tags/evidence](../labs/registry-tags/evidence): `summary.json`
(pins, commands, two raw API observations and results) and one trace per scenario. The exact
configurations are in [labs/registry-tags/configs](../labs/registry-tags/configs).

## Data and oracle

One 2-byte config blob and one layerless OCI manifest are uploaded once per repository. The
same manifest is then stored under every tag, so no layers are needed.

- `cursorproof/tags`: 24 tags, pushed in an order that is not the listing order. The names
  mix digits, uppercase, `_` and lowercase, and include `1.10` before `1.2` and `v10` before
  `v2`, which only byte-wise ordering produces.
- `cursorproof/partial`: the same without `1.2`, `alpha.1` and `zeta`. It is a control.

The expected stream comes from [fixture.py](../labs/registry-tags/fixture.py): the pushed
list, sorted in code point order. [oracle.py](../labs/registry-tags/oracle.py) prints it as
a JSON array for CursorProof's command oracle. It never calls `tags/list`.

## Run it

Docker is required. The script uses the installed `cursorproof` and PyYAML:

```sh
uv run python labs/registry-tags/run_lab.py
```

The first run pulls the pinned image. The script then pushes the tags, records the `Link`
header of a first and a last page with a plain HTTP client, runs CursorProof with page sizes
`[1, 5, 8, 24, 100]`, replays each trace offline, live-replays traversal 2 (with
`--execute-hooks`, because the oracle is a command) and rewrites the configurations and
evidence. Use `--port` or `--output` to change the port or output folder.

## Result

| Scenario | Exit | Result |
| --- | --- | --- |
| `tags` | 0 | All 5 traversals returned exactly the 24 pushed tags in order: 24, 5, 3, 1 and 1 pages |
| `tags-partial` | 1 | Control: the full oracle against the repository without three tags; CP003 names `1.2`, `alpha.1` and `zeta` |

Observed against the pinned version:

- Every non-final page has one `Link` field line with one `rel="next"` link. The target is
  root-relative and keeps `n` unchanged, so the unchanged URL-mode scope check accepts it.
- A page that ends exactly at the last tag (`n=8` and `n=24` with 24 tags) has no `Link`
  header. No traversal needed an extra empty page.
- Offline replay returns the same outcome, summary and findings as the run. Live replay of
  traversal 2 returns the same exit code.

## Limits

- The specification allows a registry to change or omit `n` in the continuation ("`n` may
  change on the second to last response or be fully omitted, depending on the server
  implementation"). Registry 3.0.0 did not do this here. A server that does is rejected with
  `Continuation URL must preserve scope parameters and page size` (exit 2): CursorProof does
  not relax URL validation for it.
- Static data only: nothing is pushed or deleted during a traversal. One node, in-memory
  storage, no authentication. Token negotiation (`WWW-Authenticate`) is not implemented;
  a fixed `Authorization` header can be configured.
- Only `tags/list` is exercised. `_catalog` uses the same pagination but is not tested here.
- Manifest contents and digests are not verified.
- Live replay needs the same pinned image and the same pushed tags.
