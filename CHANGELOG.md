# Changelog

User-visible changes are listed by release. Version links compare the tagged
source; unreleased changes are not available in the latest PyPI package.

## [Unreleased]

### Added

- Monthly Dependabot checks for Python dependencies and GitHub Actions,
  grouped minor/patch updates, a seven-day cooldown and limited open PRs.
- EditorConfig, Git file attributes, contribution and security policies,
  a code of conduct, issue forms and a pull request template.

### Changed

- Focused ignore rules for Python development, local secrets and generated
  artifacts.
- CI cancellation for superseded runs, full matrix feedback and workflow
  job timeouts.

## [0.2.0] - 2026-10-11

### Added

- Explicit continuation URL pagination alongside the default token mode.
  Links are validated against the initial origin, endpoint, scope parameters
  and selected page size before following them.
- A real Django/DRF HTTP and ORM regression example with independent oracle
  checks, control datasets, sanitized evidence and offline replay.
- Support for Python 3.10 through 3.14. The optional Django/DRF integration
  environment requires Python 3.12 or newer.

### Fixed

- Preserve opaque percent-encoded cursor bytes without decoding them as UTF-8.
- Preserve supported timezone-aware ISO date/time forms on Python 3.10.
- Use absolute README links and image URLs in package metadata.

## [0.1.2] - 2026-09-27

### Added

- Failure identity matching and virtual relationships in trace analysis.

### Fixed

- Avoid quadratic work when analyzing pagination traces.

## [0.1.1] - 2026-09-27

### Fixed

- Repair tagged release automation and modern package metadata handling
  during PyPI publication.
- Update README packaging and release actions for Node.js 24.

## [0.1.0] - 2026-09-27

### Added

- Initial release of the pagination checker, including token traversal,
  ordering and duplicate checks, independent oracles, saved traces and replay.

[Unreleased]: https://github.com/0then0/cursorproof/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/0then0/cursorproof/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/0then0/cursorproof/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/0then0/cursorproof/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/0then0/cursorproof/releases/tag/v0.1.0
