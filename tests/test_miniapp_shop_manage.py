"""Shop Mini App manage APIs: customers / orders / tickets scoped to the owner shop."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.miniapp_pages import register_miniapp_pages
from app.api.miniapp_shop_manage import register_miniapp_shop_manage
from app.db.models import (
    Base,
    BotUser,
    BotUserShop,
    Order,
    ResellerProfile,
    Ticket,
    TicketMessage,
    TicketStatus,
)
from app.services.miniapp_shop import PLATFORM_SWITCH, SHOP_OWNER_ONLY, SHOP_SWITCH, new_shop_key
from app.services.secret_box import seal_bot_token
from app.services.users import clear_settings_cache, set_setting
from tests.test_miniapp_shop_scope import MAIN_TOKEN, OTHER_SHOP_TOKEN, SHOP_TOKEN, signed

SHOP, OTHER_SHOP = 5, 6
KEY, OTHER_KEY = new_shop_key(), new_shop_key()
OWNER_TG, OTHER_OWNER_TG = 5005, 6006
CUSTOMER_TG, BOT_ADMIN_TG, ADMIN_TG = 1010, 2000, 7001
FULL_PERMS = '["dashboard","orders","payments","tickets","miniapp","shop_settings"]'
NO_TICKETS = '["dashboard","orders","payments","miniapp","shop_settings"]'
NO_ORDERS = '["dashboard","payments","tickets","miniapp","shop_settings"]'


class ShopManageMiniAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        async with self.Session() as session:
            session.add_all([
                BotUser(
                    id=SHOP, telegram_id=OWNER_TG, referral_code="o5", role="reseller", full_name="Owner",
                ),
                BotUser(
                    id=OTHER_SHOP, telegram_id=OTHER_OWNER_TG, referral_code="o6", role="reseller",
                ),
                BotUser(
                    id=10, telegram_id=CUSTOMER_TG, referral_code="c10", role="user",
                    full_name="<img src=x onerror=alert(1)>", username="cust",
                ),
                BotUser(id=11, telegram_id=BOT_ADMIN_TG, referral_code="ba11", role="user"),
                BotUser(id=20, telegram_id=ADMIN_TG, referral_code="a20", role="admin"),
                BotUser(
                    id=12, telegram_id=1212, referral_code="c12", role="user",
                    full_name="OtherShopCustomer",
                ),
                BotUserShop(bot_user_id=10, shop_id=SHOP),
                BotUserShop(bot_user_id=12, shop_id=OTHER_SHOP),
                BotUserShop(bot_user_id=OTHER_SHOP, shop_id=SHOP),
                BotUserShop(bot_user_id=SHOP, shop_id=OTHER_SHOP),
                BotUserShop(bot_user_id=20, shop_id=SHOP),
                ResellerProfile(
                    id=1, user_id=SHOP, is_active=True, setup_completed_at=now,
                    bot_token=seal_bot_token(SHOP_TOKEN), miniapp_key=KEY,
                    web_permissions=FULL_PERMS, bot_admin_ids=str(BOT_ADMIN_TG),
                    bot_username="shop5bot",
                ),
                ResellerProfile(
                    id=2, user_id=OTHER_SHOP, is_active=True, setup_completed_at=now,
                    bot_token=seal_bot_token(OTHER_SHOP_TOKEN), miniapp_key=OTHER_KEY,
                    web_permissions=FULL_PERMS,
                ),
                Order(id=20, user_id=10, amount=50_000, status="paid", reseller_id=SHOP),
                Order(id=21, user_id=12, amount=60_000, status="paid", reseller_id=OTHER_SHOP),
                Ticket(
                    id=30, user_id=10, subject="<b>Shop ticket</b>", status=TicketStatus.OPEN.value,
                    reseller_id=SHOP,
                ),
                Ticket(
                    id=31, user_id=12, subject="Other shop", status=TicketStatus.OPEN.value,
                    reseller_id=OTHER_SHOP,
                ),
                Ticket(
                    id=32, user_id=10, subject="Platform", status=TicketStatus.OPEN.value,
                    reseller_id=None,
                ),
                TicketMessage(
                    id=1, ticket_id=30, sender_id=CUSTOMER_TG, is_staff=False,
                    body="<script>alert(1)</script>",
                ),
            ])
            await session.commit()
            await set_setting(session, PLATFORM_SWITCH, "1")
            await set_setting(session, SHOP_SWITCH, "1", reseller_id=SHOP)
            await set_setting(session, SHOP_SWITCH, "1", reseller_id=OTHER_SHOP)
        clear_settings_cache()
        settings = SimpleNamespace(
            miniapp_enabled=True,
            bot_token=MAIN_TOKEN,
            currency="تومان",
            miniapp_url="https://panel.example.com/miniapp/",
        )
        self.notify = AsyncMock()
        self.open_bot = AsyncMock(return_value=(SimpleNamespace(session=SimpleNamespace(close=AsyncMock())), True))
        self.patches = [
            patch("app.services.miniapp_auth.get_settings", return_value=settings),
            patch("app.api.miniapp_pages.get_settings", return_value=settings),
            patch("app.services.miniapp_shop.get_settings", return_value=settings),
            patch("app.services.miniapp_shop_manage.notify_ticket_message", self.notify),
            patch("app.services.miniapp_shop_manage.open_notify_bot_for_reseller", self.open_bot),
        ]
        for p in self.patches:
            p.start()

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_miniapp_pages(app, render=lambda *_a, **_k: HTMLResponse("page"), get_db=get_db)
        register_miniapp_shop_manage(app, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        clear_settings_cache()
        await self.engine.dispose()

    async def get(
        self, path: str, *, tg_id: int, token: str = SHOP_TOKEN, key: str | None = KEY, **params
    ) -> httpx.Response:
        headers = {"X-Telegram-Init-Data": signed(token, tg_id)}
        if key:
            headers["X-Mini-Shop"] = key
        return await self.client.get(path, headers=headers, params=params)

    async def post(
        self, path: str, *, tg_id: int, token: str = SHOP_TOKEN, key: str | None = KEY, json=None
    ) -> httpx.Response:
        headers = {"X-Telegram-Init-Data": signed(token, tg_id)}
        if key:
            headers["X-Mini-Shop"] = key
        return await self.client.post(path, headers=headers, json=json)

    async def _set_perms(self, raw: str) -> None:
        async with self.Session() as session:
            profile = await session.get(ResellerProfile, 1)
            profile.web_permissions = raw
            await session.commit()

    async def test_owner_lists_only_own_shop_rows(self) -> None:
        customers = (await self.get("/api/mini/shop/customers", tg_id=OWNER_TG)).json()
        ids = [row["id"] for row in customers["items"]]
        self.assertIn(10, ids)
        self.assertNotIn(12, ids)
        hostile = next(row for row in customers["items"] if row["id"] == 10)
        self.assertEqual(hostile["name"], "<img src=x onerror=alert(1)>")
        orders = (await self.get("/api/mini/shop/orders", tg_id=OWNER_TG)).json()
        self.assertEqual([row["id"] for row in orders["items"]], [20])
        tickets = (await self.get("/api/mini/shop/tickets", tg_id=OWNER_TG)).json()
        self.assertEqual([row["id"] for row in tickets["items"]], [30])
        thread = (await self.get("/api/mini/shop/tickets/30", tg_id=OWNER_TG)).json()
        self.assertEqual(thread["ticket"]["subject"], "<b>Shop ticket</b>")
        self.assertEqual(thread["messages"][0]["body"], "<script>alert(1)</script>")

    async def test_non_owners_and_wrong_key_get_403(self) -> None:
        paths = (
            "/api/mini/shop/customers",
            "/api/mini/shop/orders",
            "/api/mini/shop/tickets",
            "/api/mini/shop/tickets/30",
        )
        for tg in (CUSTOMER_TG, BOT_ADMIN_TG, OTHER_OWNER_TG, ADMIN_TG):
            for path in paths:
                with self.subTest(tg=tg, path=path):
                    response = await self.get(path, tg_id=tg)
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json()["detail"], SHOP_OWNER_ONLY)
        response = await self.get(
            "/api/mini/shop/customers",
            tg_id=OWNER_TG,
            token=OTHER_SHOP_TOKEN,
            key=OTHER_KEY,
        )
        self.assertEqual(response.status_code, 403)

    async def test_missing_permission_is_403(self) -> None:
        await self._set_perms(NO_ORDERS)
        self.assertEqual((await self.get("/api/mini/shop/orders", tg_id=OWNER_TG)).status_code, 403)
        await self._set_perms(NO_TICKETS)
        self.assertEqual((await self.get("/api/mini/shop/tickets", tg_id=OWNER_TG)).status_code, 403)
        self.assertEqual((await self.get("/api/mini/shop/tickets/30", tg_id=OWNER_TG)).status_code, 403)

    async def test_customers_denied_without_dashboard_perm(self) -> None:
        from app.services.miniapp_shop_manage import ManageDenied, list_shop_customers

        profile = SimpleNamespace(is_active=True, web_permissions='["miniapp"]')
        with patch(
            "app.services.miniapp_shop_manage.shop_feature_allowed",
            return_value=False,
        ):
            with self.assertRaises(ManageDenied):
                await list_shop_customers(
                    AsyncMock(),
                    shop_id=SHOP,
                    profile=profile,
                )

    async def test_foreign_ticket_is_404(self) -> None:
        for tid in (31, 32, 999):
            with self.subTest(tid=tid):
                response = await self.get(f"/api/mini/shop/tickets/{tid}", tg_id=OWNER_TG)
                self.assertEqual(response.status_code, 404)

    async def test_reply_is_shop_scoped_notifies_and_is_idempotent(self) -> None:
        from sqlalchemy import func, select

        from app.db.models import TicketMessage

        key = "a" * 32
        ok = await self.post(
            "/api/mini/shop/tickets/30/reply",
            tg_id=OWNER_TG,
            json={"body": "پاسخ <b>امن</b>", "client_key": key},
        )
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertFalse(ok.json().get("duplicate"))
        self.notify.assert_awaited_once()
        again = await self.post(
            "/api/mini/shop/tickets/30/reply",
            tg_id=OWNER_TG,
            json={"body": "پاسخ <b>امن</b>", "client_key": key},
        )
        self.assertEqual(again.status_code, 200, again.text)
        self.assertTrue(again.json().get("duplicate"))
        self.assertEqual(self.notify.await_count, 1)
        async with self.Session() as session:
            count = await session.scalar(
                select(func.count()).select_from(TicketMessage).where(TicketMessage.ticket_id == 30)
            )
            # Seed message + one staff reply.
            self.assertEqual(int(count), 2)
            other = await session.get(Ticket, 31)
            self.assertEqual(other.status, TicketStatus.OPEN.value)
        denied = await self.post(
            "/api/mini/shop/tickets/31/reply",
            tg_id=OWNER_TG,
            json={"body": "nope", "client_key": "b" * 32},
        )
        self.assertEqual(denied.status_code, 404)

    async def test_invalid_limit_and_ids_rejected(self) -> None:
        bad_limit = await self.get("/api/mini/shop/customers", tg_id=OWNER_TG, limit=0)
        self.assertEqual(bad_limit.status_code, 422)
        huge = await self.get("/api/mini/shop/orders", tg_id=OWNER_TG, before_id=2**31)
        self.assertEqual(huge.status_code, 422)
        empty_body = await self.post(
            "/api/mini/shop/tickets/30/reply",
            tg_id=OWNER_TG,
            json={"body": "   ", "client_key": "c" * 32},
        )
        self.assertEqual(empty_body.status_code, 422)
        missing_key = await self.post(
            "/api/mini/shop/tickets/30/reply",
            tg_id=OWNER_TG,
            json={"body": "ok"},
        )
        self.assertEqual(missing_key.status_code, 422)


class OpsPayloadUnitTests(unittest.TestCase):
    def test_panel_link_table_uses_feature_perms(self) -> None:
        from app.services.miniapp_shop_ops import OPS_PANEL_LINK_PERMS
        from app.services.resellers import FEATURE_PERMS

        keys = {k for k, _ in FEATURE_PERMS}
        for link_id, perm in OPS_PANEL_LINK_PERMS.items():
            self.assertIn(perm, keys, msg=f"{link_id} → {perm}")
        self.assertEqual(OPS_PANEL_LINK_PERMS["finance"], "payments")
        self.assertEqual(OPS_PANEL_LINK_PERMS["home"], "dashboard")
        self.assertEqual(OPS_PANEL_LINK_PERMS["shop"], "shop_settings")


if __name__ == "__main__":
    unittest.main()
