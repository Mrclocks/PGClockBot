"""Mini App renewal choices: the service's own plan, then its shop's categories, never another shop's."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.miniapp_renewal import register_miniapp_renewal
from app.db.models import Base, BotUser, Plan, PlanCategory, UserService

SHOP = 900


class RenewalOptionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.addAsyncCleanup(self.engine.dispose)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.addAsyncCleanup(self.session.close)
        self.user = BotUser(id=1, telegram_id=100, referral_code="u1", role="user")
        self.session.add_all([
            self.user,
            BotUser(id=2, telegram_id=200, referral_code="u2", role="user"),
            BotUser(id=SHOP, telegram_id=9000, referral_code="shop", role="reseller"),
            PlanCategory(id=10, name="<b>اقتصادی</b>", owner_reseller_id=None, audience="users"),
            PlanCategory(id=11, name="خالی", owner_reseller_id=None, audience="users"),
            PlanCategory(id=12, name="فروشگاه", owner_reseller_id=SHOP, audience="users"),
            Plan(id=1, name="ماهانه", price=100, data_limit_gb=50, duration_days=30, category_id=10, is_active=True),
            Plan(id=2, name="ماهانه کم", price=80, data_limit_gb=20, duration_days=30, category_id=10, is_active=True),
            Plan(id=3, name="بدون دسته", price=90, data_limit_gb=40, duration_days=30, is_active=True),
            Plan(id=4, name="تست", price=0, data_limit_gb=1, duration_days=1, is_active=True, is_trial=True),
            Plan(id=5, name="پلن فروشگاه", price=70, data_limit_gb=40, duration_days=30, category_id=12,
                 owner_reseller_id=SHOP, is_active=True),
        ])
        await self.session.flush()
        self.session.add_all([
            UserService(id=70, bot_user_id=1, plan_id=1, pg_username="me70"),
            UserService(id=71, bot_user_id=2, plan_id=1, pg_username="other71"),
            UserService(id=72, bot_user_id=1, plan_id=1, pg_username="linked72", remark="linked"),
            UserService(id=73, bot_user_id=1, plan_id=5, pg_username="shop73", shop_id=SHOP),
        ])
        await self.session.commit()
        self.scope = SimpleNamespace(shop_id=None)
        for target, value in (
            ("app.api.miniapp_renewal.load_mini_user", AsyncMock(return_value=self.user)),
            ("app.api.miniapp_pages.assert_mini_force_join", AsyncMock()),
            ("app.api.miniapp_pages.get_all_settings", AsyncMock(return_value={"pay_wallet_enabled": "1"})),
            ("app.api.miniapp_renewal.mini_scope", lambda request: self.scope),
            ("app.api.miniapp_pages.mini_scope", lambda request: self.scope),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        app = FastAPI()

        async def get_db():
            yield self.session

        register_miniapp_renewal(app, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def options(self, service_id: int) -> httpx.Response:
        return await self.client.get(f"/api/mini/service/{service_id}/renewal-options")

    async def test_the_current_plan_then_this_shops_categories(self) -> None:
        response = await self.options(70)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("no-store", response.headers["cache-control"])
        data = response.json()
        self.assertTrue(data["grouped"])
        self.assertEqual((data["current"]["id"], data["current"]["relation"]), (1, "current"))
        self.assertEqual([c["id"] for c in data["categories"]], [10])
        self.assertEqual([p["id"] for p in data["categories"][0]["plans"]], [1, 2])
        self.assertEqual(data["categories"][0]["plans"][1]["relation"], "downgrade")
        self.assertEqual([p["id"] for p in data["other"]], [3])
        self.assertEqual(data["plans"], [])
        every_id = {p["id"] for c in data["categories"] for p in c["plans"]} | {p["id"] for p in data["other"]}
        self.assertNotIn(4, every_id, "a trial plan is never offered for renewal")
        self.assertNotIn(5, every_id, "another shop's plan is never offered")

    async def test_without_categories_every_plan_is_listed_current_first(self) -> None:
        for plan in (1, 2):
            (await self.session.get(Plan, plan)).category_id = None
        await self.session.commit()
        data = (await self.options(70)).json()
        self.assertFalse(data["grouped"])
        self.assertEqual([p["id"] for p in data["plans"]], [1, 2, 3])
        self.assertEqual((data["categories"], data["other"]), ([], []))

    async def test_a_shop_sees_only_its_own_plans_and_categories(self) -> None:
        self.scope = SimpleNamespace(shop_id=SHOP)
        data = (await self.options(73)).json()
        self.assertEqual(data["current"]["id"], 5)
        self.assertEqual([c["id"] for c in data["categories"]], [12])

    async def test_another_customers_or_another_shops_service_is_not_found(self) -> None:
        self.assertEqual((await self.options(71)).status_code, 404)
        self.assertEqual((await self.options(73)).status_code, 404, "a shop's service is not renewed from the platform")
        self.scope = SimpleNamespace(shop_id=SHOP)
        self.assertEqual((await self.options(70)).status_code, 404, "a platform service is not renewed from a shop")

    async def test_a_linked_service_is_refused(self) -> None:
        response = await self.options(72)
        self.assertEqual(response.status_code, 400)
        self.assertIn("متصل", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
