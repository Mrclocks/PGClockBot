#!/usr/bin/env python3
"""A local stand-in for the PasarGuard panel that answers every request the same way.

The UI regression run (scripts/ui_regression.sh) must see the same panel state on every run.
A refused connection counts as an outage and, after a few, pauses panel reads and adds a banner
part-way through a crawl. This server always answers: a login token, an admin, and 404 for
everything else. An HTTP answer is not a transport failure, so pages keep showing the same
"panel data unavailable" state for the same sections on every run.

Usage: python scripts/ui_fake_panel.py PORT
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANSWERS = {
    ("POST", "/api/admin/token"): (200, {"access_token": "ui-regression-token", "token_type": "bearer"}),
    ("GET", "/api/admin"): (200, {"username": "ui-regression", "is_sudo": True, "is_disabled": False}),
}
NOT_FOUND = (404, {"detail": "Not Found"})


class Handler(BaseHTTPRequestHandler):
    def _answer(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        status, body = ANSWERS.get((self.command, self.path.split("?", 1)[0]), NOT_FOUND)
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _answer

    def log_message(self, *_args: object) -> None:
        return


def main() -> int:
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
