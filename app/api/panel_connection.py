"""Apply the shared PasarGuard read budget to web-panel HTTP requests."""

from __future__ import annotations

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.services.pg_read_cache import begin_last_good_notice
from app.services.pg_read_resilience import finish_web_read_budget, web_read_budget


def finish_panel_authentication(request: Request) -> None:
    """Short auth reads must not impose deadlines on subsequent mutation work."""
    if request.method not in {"GET", "HEAD"}:
        finish_web_read_budget()


class PanelConnectionMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path.startswith(("/api/", "/static/")):
            await self.app(scope, receive, send)
            return
        with web_read_budget() as budget:
            scope.setdefault("state", {})["pg_read_budget"] = budget
            scope["state"]["pg_last_good"] = begin_last_good_notice()

            async def send_status(message: Message) -> None:
                if message["type"] == "http.response.start" and budget.failure:
                    headers = list(message.get("headers", []))
                    headers.append((b"x-pg-read-status", budget.failure.encode("ascii")))
                    message["headers"] = headers
                await send(message)

            await self.app(scope, receive, send_status)
