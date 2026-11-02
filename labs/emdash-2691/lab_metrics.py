"""Metrics derived from a CursorProof JSON report, shared by the lab runner and offline replay.

They are computed from the recorded trace and the oracle list inside it, not from CursorProof's
finding codes, so they can cross-check the findings: identities, duplicates, gaps and exact order.
"""

from __future__ import annotations

from collections import Counter


def traversal_metrics(traversal: dict, oracle: list) -> dict:
    observed = [item["id"] for page in traversal["pages"] for item in page["items"]]
    unique = set(observed)
    return {
        "limit": traversal["limit"],
        "repetition": traversal["repetition"],
        "stop": traversal["stop"],
        "pages": len(traversal["pages"]),
        "observed": len(observed),
        "unique": len(unique),
        "duplicates": len(observed) - len(unique),
        "missing": len(set(oracle) - unique),
        "unexpected": len(unique - set(oracle)),
        "exact_order": observed == oracle,
    }


def report_signature(report: dict) -> dict:
    """Stable, timestamp-free description of one CursorProof run."""
    oracle = report["trace"]["oracle"]
    return {
        "outcome": report["outcome"],
        "expected_items": len(oracle),
        "findings": dict(sorted(Counter(f["code"] for f in report["findings"]).items())),
        "errors": [error["message"] for error in report["errors"]],
        "traversals": [traversal_metrics(t, oracle) for t in report["trace"]["traversals"]],
    }


def is_clean(signature: dict) -> bool:
    return (
        signature["outcome"] == "pass"
        and not signature["findings"]
        and not signature["errors"]
        and all(
            t["stop"] == "terminal" and t["exact_order"] and t["pages"] > 1
            for t in signature["traversals"]
        )
    )
