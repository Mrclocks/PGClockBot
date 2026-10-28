"""Bound management form bodies before CSRF parsing and keep every response private."""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.services.integration_management_forms import MAX_FORM_BYTES


class IntegrationManagementSecurity:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/integrations" or path.startswith("/integrations/")):
            await self.app(scope, receive, send)
            return

        async def private_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "private, no-store, max-age=0"
                headers["Pragma"] = "no-cache"
                headers["Referrer-Policy"] = "no-referrer"
                headers.append("Vary", "Cookie")
            await send(message)

        if scope.get("method") != "POST":
            await self.app(scope, receive, private_send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_FORM_BYTES:
                response = PlainTextResponse("حجم درخواست بیش از حد مجاز است.", status_code=413)
                await response(scope, receive, private_send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, private_send)
