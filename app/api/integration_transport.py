"""Capture socket provenance before the server's existing proxy normalization."""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn import Config
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.services.integration_client_ip import IntegrationTransport

TRANSPORT_KEY = "pgclock.integration_transport"


class IntegrationPeerCapture:
    """Install at the server boundary with Uvicorn proxy_headers=False.

    The inner Uvicorn normalizer retains existing panel behavior. Integration
    authentication uses only the immutable pre-normalization transport snapshot.
    """

    def __init__(self, app: ASGIApp, *, trusted_hosts: str | list[str] | None = None) -> None:
        self.app = ProxyHeadersMiddleware(
            app, trusted_hosts=trusted_hosts if trusted_hosts is not None else Config(app, proxy_headers=False).forwarded_allow_ips,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope = dict(scope)
            peer = scope.get("client")
            scope[TRANSPORT_KEY] = IntegrationTransport(peer[0] if peer else None, scope.get("scheme", ""))
        await self.app(scope, receive, send)
