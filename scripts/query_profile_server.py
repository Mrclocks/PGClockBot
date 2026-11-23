#!/usr/bin/env python3
"""Serve the web panel exactly as ``python -m app.main`` does, recording the SQL of every request.

Each finished request appends one JSON line to ``QUERY_PROFILE_OUT``: method and route template,
status, statement count, database time, the statement shapes run five times or more and the data
scale named by ``QUERY_PROFILE_SCALE`` (see ``scripts/query_profile_data.py``). Background
jobs running meanwhile are not counted (see ``scripts/query_counter.py``). Static files are skipped.

Run it from the checkout under test, with that checkout first on PYTHONPATH, after
``scripts/ui_fixture.py`` has prepared it (``scripts/query_profile.sh`` does both).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(1, str(Path(__file__).resolve().parent))

from query_counter import QueryRecorder  # noqa: E402 - the scripts directory is put on the path just above

SKIPPED_PREFIXES = ("/static/", "/uploads/", "/favicon")


def route_label(scope: dict[str, Any]) -> str:
    route = scope.get("route")
    template = getattr(route, "path", None) or "(unmatched)"
    return f"{scope.get('method', 'GET')} {template}"


class RecordingApp:
    """ASGI wrapper that measures each HTTP request and writes its tally."""

    def __init__(self, app: Any, out: Path, scale: int) -> None:
        self.app = app
        self.out = out
        self.scale = scale

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path", "").startswith(SKIPPED_PREFIXES):
            await self.app(scope, receive, send)
            return
        status: dict[str, int] = {}

        async def send_with_status(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        with QueryRecorder.measure("") as tally:
            await self.app(scope, receive, send_with_status)
        tally.label = route_label(scope)
        query = scope.get("query_string", b"").decode("latin-1")
        url = scope.get("path", "") + (f"?{query}" if query else "")
        row = {**tally.as_dict(), "path": scope.get("path", ""), "url": url, "status": status.get("code", 0),
               "scale": self.scale}
        with self.out.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    import uvicorn

    from app import main as app_main
    from app.db.session import engine

    out = Path(os.environ.get("QUERY_PROFILE_OUT", "query-profile-web.jsonl"))
    scale = int(os.environ.get("QUERY_PROFILE_SCALE", "0"))
    recorder = QueryRecorder(engine)
    serve = uvicorn.run

    def run_recorded(app: Any, **options: Any) -> None:
        serve(RecordingApp(app, out, scale), **options)

    app_main.uvicorn.run = run_recorded
    try:
        app_main.main()
    finally:
        recorder.close()


if __name__ == "__main__":
    main()
