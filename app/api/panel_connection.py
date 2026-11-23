"""Apply the shared PasarGuard read budget to web-panel HTTP requests."""

from __future__ import annotations

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.services.pg_read_cache import begin_last_good_notice
from app.services.pg_read_resilience import finish_web_read_budget, web_read_budget
from app.services.setup_wizard import normalize_webhook_path

# Automatic polling: honours a read pause but cannot start one for everyone.
BACKGROUND_READ_PATHS = frozenset({"/pg/metrics"})
# Not pages: the Mini App JSON API and assets, payment gateway returns and webhooks, and the
# Telegram webhook. Payments and deliveries they start keep the panel client's own timeouts;
# a page's deadline or a read pause caused by a page must never refuse them.
UNBUDGETED_PATH_PREFIXES = ("/api/", "/static/", "/payments/settlement/", "/telegram/")


def is_unbudgeted_path(path: str, *, webhook_path: str) -> bool:
    """True for requests that are not web-panel pages (see ``UNBUDGETED_PATH_PREFIXES``)."""
    return path.startswith(UNBUDGETED_PATH_PREFIXES) or path == webhook_path


def finish_panel_authentication(request: Request) -> None:
    """Short auth reads must not impose deadlines on subsequent mutation work."""
    if request.method not in {"GET", "HEAD"}:
        finish_web_read_budget()


class PanelConnectionMiddleware:
    def __init__(self, app: ASGIApp, *, webhook_path: str | None = None) -> None:
        self.app = app
        # WEBHOOK_PATH may be customised; a change takes effect on restart, like the route itself.
        self.webhook_path = normalize_webhook_path(
            get_settings().webhook_path if webhook_path is None else webhook_path
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or is_unbudgeted_path(path, webhook_path=self.webhook_path):
            await self.app(scope, receive, send)
            return
        with web_read_budget(background=path in BACKGROUND_READ_PATHS) as budget:
            scope.setdefault("state", {})["pg_read_budget"] = budget
            scope["state"]["pg_last_good"] = begin_last_good_notice()

            async def send_status(message: Message) -> None:
                if message["type"] == "http.response.start" and budget.failure:
                    headers = list(message.get("headers", []))
                    headers.append((b"x-pg-read-status", budget.failure.encode("ascii")))
                    message["headers"] = headers
                await send(message)

            await self.app(scope, receive, send_status)
