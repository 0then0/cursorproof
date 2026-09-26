"""A local API where every page returns 200, but a timestamp-only cursor loses rows."""

import argparse
import base64
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

ITEMS = [{"id": i, "created_at": i // 4} for i in range(40, 0, -1)]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/oracle":
            self.reply([item["id"] for item in ITEMS])
            return
        if parts.path not in {"/broken", "/correct"}:
            self.reply({"error": "not found"}, 404)
            return
        params = parse_qs(parts.query)
        try:
            limit = int(params.get("limit", ["5"])[0])
            if limit < 1:
                raise ValueError
            cursor = params.get("cursor", [None])[0]
            boundary = json.loads(base64.urlsafe_b64decode(cursor)) if cursor else None
            remaining = ITEMS
            if boundary is not None:
                remaining = [
                    item
                    for item in ITEMS
                    if (
                        item["created_at"] < boundary[0]
                        if parts.path == "/broken"
                        else (item["created_at"], item["id"]) < tuple(boundary)
                    )
                ]
            page = remaining[:limit]
            has_more = len(remaining) > limit
            next_cursor = None
            if has_more:
                last = page[-1]
                next_cursor = base64.urlsafe_b64encode(
                    json.dumps([last["created_at"], last["id"]]).encode()
                ).decode()
            self.reply({"results": page, "next_cursor": next_cursor, "has_more": has_more})
        except (ValueError, TypeError, IndexError):
            self.reply({"error": "invalid cursor or limit"}, 400)

    def reply(self, body: object, status: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    print(f"Demo API: http://127.0.0.1:{args.port}", flush=True)
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
