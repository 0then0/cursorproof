# CursorProof

**Check that cursor pagination returns the correct stream, not just successful pages.**

Every request can return `200 OK` while the traversal loses or duplicates records, or
enters a cursor cycle. CursorProof checks the complete traversal and can compare it
with an independent oracle.

## Install

Requires Python 3.13 or newer.

```sh
pipx install cursorproof
# or
python -m pip install cursorproof
```

## Try the demo

Install from the checkout with [uv](https://docs.astral.sh/uv/):

```sh
uv sync
```

To install the checkout as an isolated CLI tool instead, run `uv tool install .`.

Start the demo API in one terminal:

```sh
uv run python examples/demo_api.py
```

In another terminal, run the broken example:

```sh
uv run cursorproof run examples/broken.yml
```

Replay its saved trace offline with:

```sh
uv run cursorproof replay cursorproof-repro.json
```

The demo has 40 records with repeated sort values. `/broken` continues using only
the timestamp and skips records at page boundaries despite successful responses.
CursorProof reports `CP003 MISSING_ITEMS` and differences between limits. The
`/correct` endpoint also uses the unique ID; try it with:

```sh
uv run cursorproof run examples/correct.yml --format json --repro correct-repro.json
```

Validate a configuration without making requests with:

```sh
uv run cursorproof check examples/correct.yml
```

See `uv run cursorproof --help` for all commands.

## Configure your API

CursorProof accepts YAML and JSON configurations. The [user guide](https://github.com/0then0/cursorproof/blob/main/docs/guide.md#configuration)
covers response paths, ordering, defaults, and validation.

An independent oracle lets CursorProof detect missing and unexpected records.
Without one, CursorProof can check internal consistency and ordering, but **cannot
establish completeness**. See [Oracle](https://github.com/0then0/cursorproof/blob/main/docs/guide.md#oracle) for HTTP and command examples.

## Checks and safety

CursorProof supports static traversals, snapshots, live keyset checks, mutation hooks,
boundary testing, and cursor binding. These checks can make real requests or execute
configured commands. Review the [user guide](https://github.com/0then0/cursorproof/blob/main/docs/guide.md) before using mutation or
boundary commands against data you care about.

The guide also documents [findings and exit codes](https://github.com/0then0/cursorproof/blob/main/docs/guide.md#findings-and-exit-codes)
and [trace replay and privacy](https://github.com/0then0/cursorproof/blob/main/docs/guide.md#reproduction-and-privacy).

## Development

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
```

The core analyzer has no network I/O. The runner extracts observations and the CLI
renders reports. Tests use HTTPX transports and Hypothesis, plus a local demo API.
