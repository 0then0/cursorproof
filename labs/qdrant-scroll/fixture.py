"""Known point IDs and payloads for the Qdrant Scroll lab (standard library only).

The expected stream is derived here, from the construction rule, never read from Qdrant.
"""

from __future__ import annotations

import uuid

INTS = "cursorproof_ints"
UUIDS = "cursorproof_uuids"
FILTER = {"must": [{"key": "group", "match": {"value": "keep"}}]}
VECTOR_SIZE = 4


def _payload(position: int) -> dict[str, object]:
    # Every third point is excluded by the filter; `rank` is a distractor payload field.
    return {"group": "drop" if position % 3 == 2 else "keep", "rank": position}


def int_points() -> list[tuple[int, dict[str, object]]]:
    ids = [*range(0, 40), 2**53 + 1, 2**53 + 2, 2**63, 2**64 - 2]
    return [(point_id, _payload(position)) for position, point_id in enumerate(ids)]


def uuid_points() -> list[tuple[str, dict[str, object]]]:
    ids = [str(uuid.uuid5(uuid.NAMESPACE_URL, f"cursorproof/qdrant/{n}")) for n in range(44)]
    return [(point_id, _payload(position)) for position, point_id in enumerate(ids)]


def expected_ints() -> list[int]:
    return sorted(point_id for point_id, payload in int_points() if payload["group"] == "keep")


def expected_uuids() -> list[str]:
    # Qdrant scrolls in point ID order; UUIDs are ordered by their unsigned 128-bit value.
    kept = [point_id for point_id, payload in uuid_points() if payload["group"] == "keep"]
    return sorted(kept, key=lambda value: uuid.UUID(value).int)
