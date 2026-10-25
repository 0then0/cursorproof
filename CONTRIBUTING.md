# Contributing to CursorProof

Bug reports, documentation improvements and focused pull requests are welcome.
Read the [code of conduct](CODE_OF_CONDUCT.md) before participating. Report
vulnerabilities privately using the [security policy](SECURITY.md).

## Report a problem or propose a change

Search [existing issues](https://github.com/0then0/cursorproof/issues) first.
For bugs, include the CursorProof and Python versions, a minimal configuration,
steps to reproduce, and the expected and actual results. Remove credentials,
private data and raw cursors from configurations, logs and traces before sharing.
Automatic trace redaction is not a substitute for reviewing attachments.

For substantial features or changes to public behavior, describe the use case
in an issue before implementing them. Small fixes can go directly to a PR.

## Set up a checkout

Use Python 3.10 or newer and [uv](https://docs.astral.sh/uv/). CI uses uv 0.12.18.

```sh
git clone https://github.com/0then0/cursorproof.git
cd cursorproof
uv sync --locked
```

Keep `uv.lock` committed. Change dependency declarations and regenerate the
lockfile together only when dependency changes are part of your contribution.
Do not commit virtual environments, generated traces or credentials.

## Make and verify a change

Keep changes focused and follow the existing code. The runner records HTTP
observations, the analyzer evaluates evidence without network I/O, and the CLI
loads configuration and displays reports. Preserve token-mode compatibility,
trace privacy and the distinction between complete and incomplete traversals.

Add a regression test for a bug or a meaningful behavioral test for a feature.
Documentation-only changes do not require new application tests. Update the
[guide](docs/guide.md), examples and [changelog](CHANGELOG.md) when relevant.

Run targeted tests while developing, then the checks used by CI:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
```

To apply Python formatting, use `uv run ruff format .`. EditorConfig defines
UTF-8, LF line endings, four-space Python indentation and two-space YAML,
JSON and TOML indentation. Ruff is authoritative for Python formatting.

Core tests use local fixtures and do not need an external API. CI runs on
Python 3.10 through 3.14. The optional Django/DRF tests require Python 3.12 or
newer and are skipped when those frameworks are absent. Follow the
[integration instructions](docs/drf-regression.md#run-the-example) to test the
built wheel in a separate environment with a disposable SQLite database.

Use controlled local resources for tests that execute fixture commands or
mutation hooks. Do not run them against shared or production data.

## Submit a pull request

Explain the problem, the resulting behavior and the checks you actually ran.
Link the issue when one exists and identify any compatibility changes or
unverified behavior. Avoid unrelated refactoring or dependency updates.

Use Conventional Commits in English, for example `fix: preserve opaque cursors`
or `docs: clarify replay commands`. Supported types are `feat`, `fix`,
`refactor`, `chore`, `docs` and `test`. Maintainers review PRs before merging;
dependency PRs are not merged automatically.

Contributions are distributed under the project's [Apache-2.0 license](LICENSE).

## Maintainer releases

Update the package version and changelog, run the checks above and wait for CI
on the intended commit. An annotated `vX.Y.Z` tag must match the version in
`pyproject.toml`. Pushing that tag starts the release workflow, which verifies
the package, builds a wheel and source archive, creates a GitHub Release and
publishes the same distributions to PyPI using trusted publishing.

The manual workflow accepts an existing tag and publishes to PyPI only when
`publish_to_pypi` is selected. Never replace an existing release tag or reuse
a published PyPI version; publish a new version for corrections.
