"""Known tags for the Distribution Registry lab (standard library only).

The expected stream is derived here, from the tag list that is pushed, never read from the
registry's own tags/list endpoint.
"""

from __future__ import annotations

REPOSITORY = "cursorproof/tags"
PARTIAL_REPOSITORY = "cursorproof/partial"

# Pushed in this order, which is deliberately not the listing order. The names put digits,
# uppercase, `_` and lowercase next to each other and include numeric-looking traps
# ("1.10" before "1.2", "v10" before "v2") that only a byte-wise comparison orders this way.
TAGS = [
    "zeta", "v2", "latest", "1.10", "alpha", "Zulu", "v10", "stable", "0.9", "alpha_1", "edge",
    "RC1", "2", "beta", "alpha.1", "v1.0.0", "_internal", "10", "nightly", "1.2", "Beta",
    "alpha-1", "v1", "1.0",
]  # fmt: skip
# The control repository lacks these; the full oracle must then report them as missing.
WITHHELD = ["1.2", "alpha.1", "zeta"]


def expected_tags() -> list[str]:
    # The Registry API lists tags "ordered lexically": Unicode code point order of ASCII names.
    return sorted(TAGS)


def partial_tags() -> list[str]:
    return [tag for tag in TAGS if tag not in WITHHELD]
