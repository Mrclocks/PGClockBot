"""A shop's Mini App sells only that shop's plans, from that shop's purse, as that shop's orders."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import func, select

from app.db.models import BotUser, Order, Plan, ShopWallet
from app.services.users import clear_settings_cache, current_shop_reseller_id, set_setting
from tests.test_miniapp_shop_readonly import CUSTOMER, KEY, OTHER_SHOP, SHOP, ShopFixture
from tests.test_miniapp_shop_scope import MAIN_TOKEN, SHOP_TOKEN, signed

SHOP_PLAN, PLATFORM_PLAN, OTHER_SHOP_PLAN, SHOP_TRIAL = 51, 52, 53, 54


class ShopPurchaseTests(ShopFixture):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        async with self.Session() as session:
            session.add_all([
                Plan(id=SHOP_PLAN, name="shop plan", price=5_000, duration_days=30, data_limit_gb=10,
                     owner_reseller_id=SHOP, is_active=True),
                Plan(id=PLATFORM_PLAN, name="platform plan", price=3_000, duration_days=30, data_limit_gb=10,
                     is_active=True),
                Plan(id=OTHER_SHOP_PLAN, name="other shop plan", price=1_000, duration_days=30, data_limit_gb=10,
                     owner_reseller_id=OTHER_SHOP, is_active=True),
                Plan(id=SHOP_TRIAL, name="shop trial", price=0, duration_days=1, data_limit_gb=1,
                     owner_reseller_id=SHOP, is_active=True, is_trial=True),
            ])
            await session.commit()
            for shop in (None, SHOP):
                await set_setting(session, "pay_wallet_enabled", "1", reseller_id=shop)
        clear_settings_cache()
        self.delivered = AsyncMock(side_effect=lambda _session, order, *a, **k: order)
        for p in (
            patch("app.services.orders.fulfill_paid_order", self.delivered),
            patch("app.services.orders._assert_plan_volume_available", AsyncMock()),
        ):
            p.start()
            self.patches.append(p)

    async def post(self, path: str, body: dict, *, key: str | None = KEY, token: str = SHOP_TOKEN):
        headers = {"X-Telegram-Init-Data": signed(token, CUSTOMER)}
        if key:
            headers["X-Mini-Shop"] = key
        return await self.client.post(path, json=body, headers=headers)

    async def purses(self) -> tuple[int, int]:
        async with self.Session() as session:
            platform = int((await session.get(BotUser, 10)).wallet_balance)
            shop = await session.scalar(select(ShopWallet.balance).where(ShopWallet.user_id == 10, ShopWallet.reseller_id == SHOP))
            return platform, int(shop or 0)

    async def test_the_catalog_is_the_shops_own(self) -> None:
        plans = (await self.shop_get("/api/mini/plans")).json()["plans"]
        self.assertEqual([p["id"] for p in plans], [SHOP_TRIAL, SHOP_PLAN])

    async def test_buying_pays_from_the_shop_purse_and_records_a_shop_order(self) -> None:
        response = await self.post("/api/mini/buy", {"plan_id": SHOP_PLAN, "price": 5_000})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["wallet"], 2_000)
        self.assertEqual(await self.purses(), (50_000, 2_000), "the platform purse is untouched")
        async with self.Session() as session:
            order = await session.scalar(select(Order))
        self.assertEqual((order.reseller_id, order.plan_id), (SHOP, SHOP_PLAN))
        self.delivered.assert_awaited_once()

    async def test_plans_of_the_platform_or_another_shop_cannot_be_bought(self) -> None:
        for plan_id in (PLATFORM_PLAN, OTHER_SHOP_PLAN, 999):
            with self.subTest(plan_id=plan_id):
                response = await self.post("/api/mini/buy", {"plan_id": plan_id})
                self.assertEqual(response.status_code, 400)
        async with self.Session() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(Order)), 0)
        self.assertEqual(await self.purses(), (50_000, 7_000))

    async def test_the_shop_purse_must_cover_the_price(self) -> None:
        async with self.Session() as session:
            await session.execute(ShopWallet.__table__.update().values(balance=100))
            await session.commit()
        response = await self.post("/api/mini/buy", {"plan_id": SHOP_PLAN, "price": 5_000})
        self.assertEqual(response.status_code, 400, "50,000 in the platform purse does not pay a shop order")

    async def test_wallet_payment_follows_the_shops_own_switch(self) -> None:
        async with self.Session() as session:
            await set_setting(session, "pay_wallet_enabled", "0", reseller_id=SHOP)
        clear_settings_cache()
        self.assertEqual((await self.post("/api/mini/buy", {"plan_id": SHOP_PLAN})).status_code, 403)
        me = (await self.shop_get("/api/mini/me")).json()
        self.assertFalse(me["customer"]["wallet_pay_enabled"])

    async def test_renewal_and_addons_need_a_service_the_shop_sold(self) -> None:
        for path in ("/api/mini/service/102/renewal-preview?plan_id=51", "/api/mini/service/103/addons"):
            with self.subTest(path=path):
                self.assertEqual((await self.shop_get(path)).status_code, 404)
        for path, body in (("/api/mini/renew", {"service_id": 102, "plan_id": SHOP_PLAN}),
                           ("/api/mini/addon", {"service_id": 103, "pack_id": 1})):
            with self.subTest(path=path):
                self.assertEqual((await self.post(path, body)).status_code, 404)

    async def test_platform_channels_do_not_gate_a_shop_purchase(self) -> None:
        platform_gate = AsyncMock(side_effect=HTTPException(403, "join the platform channel"))
        with patch("app.api.miniapp_pages.assert_mini_force_join", platform_gate):
            shop = await self.post("/api/mini/buy", {"plan_id": SHOP_PLAN, "price": 5_000})
            platform = await self.post("/api/mini/buy", {"plan_id": PLATFORM_PLAN}, key=None, token=MAIN_TOKEN)
        self.assertEqual((shop.status_code, platform.status_code), (200, 403))
        platform_gate.assert_awaited_once()

    async def test_each_request_sets_its_own_shop_context(self) -> None:
        await self.shop_get("/api/mini/plans")
        platform = await self.client.get(
            "/api/mini/plans", headers={"X-Telegram-Init-Data": signed(MAIN_TOKEN, CUSTOMER)}
        )
        self.assertEqual([p["id"] for p in platform.json()["plans"]], [PLATFORM_PLAN])
        self.assertIsNone(current_shop_reseller_id())


if __name__ == "__main__":
    unittest.main()
