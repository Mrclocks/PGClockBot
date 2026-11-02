"""Security checks for finance receipt proxy scoping."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, Order, Payment, ResellerProfile
from app.services.receipt_files import (
    RECEIPT_MAX_BYTES,
    fetch_telegram_file,
    payment_bot_token,
    safe_telegram_file_path,
)
from app.services.secret_box import seal_bot_token



class ReceiptPathSafetyTests(unittest.TestCase):
    def test_accepts_normal_telegram_paths(self):
        self.assertEqual(
            safe_telegram_file_path("photos/file_123.jpg"),
            "photos/file_123.jpg",
        )
        self.assertEqual(
            safe_telegram_file_path("/photos/file_123.jpg"),
            "photos/file_123.jpg",
        )

    def test_rejects_traversal_and_schemes(self):
        self.assertIsNone(safe_telegram_file_path("../etc/passwd"))
        self.assertIsNone(safe_telegram_file_path("photos/../../etc/passwd"))
        self.assertIsNone(safe_telegram_file_path("https://evil.example/a.jpg"))
        self.assertIsNone(safe_telegram_file_path("file:foo"))
        self.assertIsNone(safe_telegram_file_path("photos\\file.jpg"))
        self.assertIsNone(safe_telegram_file_path("photos/file.jpg?x=1"))
        self.assertIsNone(safe_telegram_file_path(""))
        self.assertIsNone(safe_telegram_file_path(None))


class ReceiptSourceGuardsTests(unittest.TestCase):
    def test_receipt_route_has_tenant_guards(self):
        src = open("app/api/finance_pages.py", encoding="utf-8").read()
        self.assertIn("_authorize_receipt_access", src)
        self.assertIn("payer_reseller_id", src)
        self.assertIn("no-store", src)
        # Platform payments list excludes reseller tenants (top-ups by the wallet's shop).
        self.assertIn("Payment.wallet_shop_id.is_(None)", src)


class ReceiptBotTokenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add(ResellerProfile(user_id=3, bot_token=seal_bot_token("SHOP_TOKEN")))
        self.session.add(ResellerProfile(user_id=5, bot_token=None))
        await self.session.flush()
        self.platform = patch("app.services.receipt_files.get_settings", return_value=SimpleNamespace(bot_token="PLATFORM"))
        self.platform.start()

    async def asyncTearDown(self) -> None:
        self.platform.stop()
        await self.session.close()
        await self.engine.dispose()

    async def token(self, *, order_shop=None, payer_shop=None) -> str:
        order = Order(id=1, user_id=1, amount=1, reseller_id=order_shop) if order_shop is not None else None
        return await payment_bot_token(self.session, Payment(id=1, user_id=1, amount=1), order=order, payer_reseller_id=payer_shop)

    async def test_each_receipt_is_read_with_its_own_bots_token(self) -> None:
        self.assertEqual(await self.token(), "PLATFORM")
        self.assertEqual(await self.token(order_shop=3), "SHOP_TOKEN")
        self.assertEqual(await self.token(payer_shop=3), "SHOP_TOKEN")

    async def test_shop_without_a_bot_never_falls_back_to_the_platform_token(self) -> None:
        self.assertEqual(await self.token(order_shop=5), "")
        self.assertEqual(await self.token(payer_shop=99), "")


class FetchReceiptFileTests(unittest.IsolatedAsyncioTestCase):
    def client(self, *, file_path="photos/a.jpg", content=b"img", ctype="image/jpeg") -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/getFile"):
                return httpx.Response(200, json={"ok": True, "result": {"file_path": file_path}})
            return httpx.Response(200, content=content, headers={"content-type": ctype})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fetch(self, **kw):
        async with self.client(**kw) as client:
            return await fetch_telegram_file("T", "F", client=client)

    async def test_returns_image_bytes_with_their_type(self) -> None:
        self.assertEqual(await self.fetch(), (b"img", "image/jpeg"))
        self.assertEqual(await self.fetch(file_path="photos/a.png", ctype="application/octet-stream"), (b"img", "image/png"))

    async def test_refuses_non_images_unsafe_paths_and_oversized_files(self) -> None:
        self.assertIsNone(await self.fetch(ctype="text/html"))
        self.assertIsNone(await self.fetch(file_path="../secret"))
        self.assertIsNone(await self.fetch(content=b"x" * (RECEIPT_MAX_BYTES + 1)))


if __name__ == "__main__":
    unittest.main()
