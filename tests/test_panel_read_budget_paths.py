"""Only web-panel pages get the panel read budget; gateway, webhook and Telegram requests do not."""

from __future__ import annotations

import unittest

from app.api.panel_connection import PanelConnectionMiddleware
from app.services.pg_read_resilience import _BUDGET


async def run(path: str, *, webhook_path: str = "/telegram/webhook") -> object:
    seen = {}

    async def endpoint(scope, receive, send) -> None:
        seen["budget"] = _BUDGET.get()

    middleware = PanelConnectionMiddleware(endpoint, webhook_path=webhook_path)
    await middleware({"type": "http", "path": path}, None, None)
    return seen["budget"]


class ReadBudgetPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_pages_keep_the_budget(self) -> None:
        for path in ("/", "/users", "/pg/users", "/finance"):
            with self.subTest(path):
                self.assertIsNotNone(await run(path))

    async def test_payment_and_telegram_requests_have_none(self) -> None:
        paths = (
            "/telegram/webhook",
            "/payments/settlement/psp/zarinpal/return/7",
            "/payments/settlement/card-auto/shop/3/webhook",
            "/payments/settlement/bbc/tenant/0/callback/9",
            "/api/mini/renew",
            "/static/app.css",
        )
        for path in paths:
            with self.subTest(path):
                self.assertIsNone(await run(path))

    async def test_a_custom_webhook_path_is_exempt(self) -> None:
        self.assertIsNone(await run("/hook/abc", webhook_path="hook/abc/"))
        self.assertIsNotNone(await run("/hook/other", webhook_path="/hook/abc"))


if __name__ == "__main__":
    unittest.main()
