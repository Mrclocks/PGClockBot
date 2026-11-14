"""The Mini App lists a customer's service packs; a pack without a category reaches every service."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.miniapp_pages import register_miniapp_pages
from app.db.models import Base, BotUser, Plan, PlanCategory, ServiceAddonPack, UserService


class MiniAddonPackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=1, telegram_id=101, referral_code="u1", role="user"),
            BotUser(id=2, telegram_id=102, referral_code="u2", role="user"),
            PlanCategory(id=1, name="VIP", audience="users"),
            Plan(id=1, name="vip", price=1, category_id=1),
            Plan(id=2, name="bare", price=1),
            UserService(id=10, bot_user_id=1, plan_id=1, pg_user_id=1, pg_username="vip-svc"),
            UserService(id=11, bot_user_id=1, plan_id=2, pg_user_id=2, pg_username="bare-svc"),
            ServiceAddonPack(id=1, name="flagged", kind="volume", amount=5, price=800, is_general=True),
            ServiceAddonPack(id=2, name="unflagged", kind="duration", amount=7, price=700, is_general=False),
            ServiceAddonPack(id=3, name="vip only", kind="volume", amount=9, price=900, category_id=1, is_general=False),
        ])
        await self.session.commit()
        app = FastAPI()

        async def db():
            yield self.session

        register_miniapp_pages(app, render=lambda *args: None, get_db=db)
        self.user_id = 1
        async def load_user(*_args):
            return await self.session.get(BotUser, self.user_id)

        self.patch = patch("app.api.miniapp_pages.load_mini_user", AsyncMock(side_effect=load_user))
        self.patch.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.patch.stop()
        await self.session.close()
        await self.engine.dispose()

    async def names(self, service_id: int, kind: str) -> list[str]:
        response = await self.client.get(f"/api/mini/service/{service_id}/addons?kind={kind}")
        self.assertEqual(response.status_code, 200)
        return [p["name"] for p in response.json()["packs"]]

    async def test_packs_without_a_category_show_for_categorised_and_plain_services(self) -> None:
        for service_id in (10, 11):
            self.assertIn("flagged", await self.names(service_id, "volume"))
            self.assertEqual(await self.names(service_id, "duration"), ["unflagged"])

    async def test_a_category_pack_shows_only_for_its_category(self) -> None:
        self.assertIn("vip only", await self.names(10, "volume"))
        self.assertNotIn("vip only", await self.names(11, "volume"))

    async def test_another_customers_service_is_not_listed(self) -> None:
        self.user_id = 2
        response = await self.client.get("/api/mini/service/10/addons?kind=volume")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
