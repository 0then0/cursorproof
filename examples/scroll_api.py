"""A local Qdrant Scroll-shaped API for POST pagination with JSON bodies.

Every page is a `POST` with the same filter and body fields. Only `limit` and `offset` may differ.
The server answers 400 and records a violation if the filter or another fixed field changes, if
an unknown field appears, or if `offset` has the wrong JSON type (`1` is not `"1"`).

Datasets, selected by path (`/scroll/<name>`):

    int      ids 1..30
    zero     ids 0..29; an empty first page returns the cursor `0`
    big      integer ids above 2**53 up to the uint64 maximum (kept by the filter)
    uuid     UUID strings in unsigned 128-bit order
    digits   string ids made of digits ("1", "2", ...); an integer offset is rejected

Faulty variants of `int`: `dup` repeats an id on the next page, `skip` omits an id at a page
boundary and `cycle` sends the client back to an earlier cursor.
"""

import argparse
import json
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

FILTER = {"must": [{"key": "group", "match": {"value": "keep"}}]}
FIXED = {"filter": FILTER, "with_payload": ["group"], "with_vector": False}
BIG_BASE = 2**53 + 1
UINT64_MAX = 2**64 - 1


def _dataset(name: str) -> list[tuple[int | str, dict[str, Any]]]:
    if name == "zero":
        ids: list[int | str] = list(range(0, 30))
    elif name == "big":
        ids = [BIG_BASE + 7 * index for index in range(28)] + [UINT64_MAX]
    elif name == "uuid":
        ids = sorted(
            (str(uuid.uuid5(uuid.NAMESPACE_URL, f"cursorproof/{index}")) for index in range(30)),
            key=lambda value: uuid.UUID(value).int,
        )
    elif name == "digits":
        ids = [str(index) for index in range(1, 31)]
    else:
        ids = list(range(1, 31))
    # Every third point is excluded by the filter, so a lost filter changes the result.
    return [
        (item, {"group": "drop" if index % 3 == 2 else "keep"}) for index, item in enumerate(ids)
    ]


def expected_ids(name: str) -> list[int | str]:
    """The independent expected stream for the fixed filter."""
    return [item for item, payload in _dataset(name) if payload["group"] == "keep"]


def endpoint(path: str) -> tuple[str, str] | None:
    parts = path.split("/")
    if len(parts) != 3 or parts[1] != "scroll":
        return None
    name = parts[2]
    fault = name if name in {"dup", "skip", "cycle"} else ""
    dataset = "int" if fault else name
    if dataset not in {"int", "zero", "big", "uuid", "digits"}:
        return None
    return dataset, fault


class Handler(BaseHTTPRequestHandler):
    violations: list[str] = []
    requests: list[dict[str, Any]] = []

    def do_GET(self) -> None:
        # The independent oracle: ids that satisfy the fixed filter, in stream order.
        found = endpoint(self.path.replace("/oracle/", "/scroll/", 1))
        if self.path.startswith("/oracle/") and found:
            self.reply(expected_ids(self.path.split("/")[2]))
        else:
            self.reply({"error": "not found"}, 404)

    def do_POST(self) -> None:
        found = endpoint(self.path)
        if found is None:
            self.reply({"error": "not found"}, 404)
            return
        dataset, fault = found
        try:
            length = int(self.headers.get("Content-Length", ""))
            body = json.loads(self.rfile.read(length))
        except ValueError:
            self.reject("body is not JSON")
            return
        problem = self.check(body, dataset)
        self.requests.append(body)
        if problem:
            self.reject(problem)
            return
        points = [
            (item, payload) for item, payload in _dataset(dataset) if payload["group"] == "keep"
        ]
        limit = body["limit"]
        stream = [item for item, _ in points]
        if "offset" not in body:
            start = 0
            if dataset == "zero":
                # An empty first page that hands out the cursor 0.
                self.reply(self.page([], 0))
                return
        else:
            start = stream.index(body["offset"])
        selected = points[start : start + limit]
        following = start + limit
        if fault == "skip" and start > 0:
            selected = points[start + 1 : start + limit + 1]
            following = start + limit + 1
        if fault == "dup" and start > 0:
            selected = points[start - 1 : start + limit - 1]
            following = start + limit - 1
        if fault == "cycle" and following >= 3 * limit:
            following = limit
        next_offset = stream[following] if following < len(stream) else None
        self.reply(self.page(selected, next_offset))

    @staticmethod
    def page(points: list[Any], next_offset: int | str | None) -> dict[str, Any]:
        return {
            "result": {
                "points": [{"id": item, "payload": payload} for item, payload in points],
                "next_page_offset": next_offset,
            },
            "status": "ok",
            "time": 0.0,
        }

    def check(self, body: object, dataset: str) -> str | None:
        if not isinstance(body, dict):
            return "body is not an object"
        extra = set(body) - {*FIXED, "limit", "offset"}
        if extra:
            return f"unexpected body fields: {sorted(extra)}"
        for name, value in FIXED.items():
            if body.get(name, "<missing>") != value:
                return f"fixed field changed: {name}"
        limit = body.get("limit")
        if type(limit) is not int or limit < 1:
            return "limit must be a positive integer"
        if "offset" in body:
            offset = body["offset"]
            wanted = str if dataset in {"uuid", "digits"} else int
            if type(offset) is not wanted:
                return f"offset must be {wanted.__name__}, not {type(offset).__name__}"
            if offset not in expected_ids(dataset):
                return "offset is not a point id in the filtered collection"
        return None

    def reject(self, message: str) -> None:
        self.violations.append(message)
        self.reply({"status": {"error": message}}, 400)

    def reply(self, body: object, status: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    print(f"Scroll API: http://127.0.0.1:{args.port}", flush=True)
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
