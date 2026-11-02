"""«اشتراک من» in the web panel: owner-only, own shop only, one renewal per submitted page."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.reseller_subscription_pages import register_reseller_subscription_pages
from app.db.models import Base, BotUser, PgAdminSubscription, ResellerPlan, ResellerProfile
from app.services.billing import GB
from app.services.reseller_package import STALE_PAGE

PERMS = '["orders","payments"]'


def plan(plan_id: int, *, users: int, gb: int, price: int, **extra) -> ResellerPlan:
    fields = {
        "plan_kind": "subscription", "billing_mode": "fixed", "duration_days": 30, "web_permissions": PERMS,
        "pg_role_id": 2, "pg_group_ids": "1", "create_pg_admin": True, "is_active": True, "is_system": False, **extra,
    }
    return ResellerPlan(id=plan_id, name=f"plan{plan_id}", included_users=users, included_gb=gb, price=price, sort_order=plan_id, **fields)


class MySubscriptionPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=3, telegram_id=33, referral_code="s3", role="reseller", wallet_balance=1_000_000),
                BotUser(id=4, telegram_id=44, referral_code="s4", role="reseller", wallet_balance=1_000_000),
                plan(1, users=10, gb=100, price=100_000, allow_buy_extra=True, extra_gb_price=1_000, extra_user_price=5_000),
                plan(2, users=20, gb=200, price=200_000, allow_buy_extra=True, extra_gb_price=1_000, extra_user_price=5_000),
                plan(3, users=50, gb=500, price=500_000, pg_role_id=9),
                ResellerProfile(id=1, user_id=3, pg_admin_username="shop3", plan_id=1, web_permissions=PERMS, is_active=True),
                ResellerProfile(id=2, user_id=4, pg_admin_username="shop4", plan_id=1, web_permissions=PERMS, is_active=True),
                PgAdminSubscription(pg_username="shop3", plan_id=1, access_status="active", base_users=10, base_gb=100, renew_generation=2),
                PgAdminSubscription(pg_username="shop4", plan_id=1, access_status="active", base_users=10, base_gb=100, renew_generation=7),
            ])
            await session.commit()
        self.staff = {"role": "reseller", "bot_user_id": 3}
        self.pg = SimpleNamespace(
            get_admin=AsyncMock(return_value={"total_users": 4, "used_traffic": 5 * GB, "data_limit": 100 * GB, "max_users": 10}),
            modify_admin=AsyncMock(return_value={}),
            get_users=AsyncMock(return_value={"users": []}),
        )
        self.rendered: list[tuple[str, dict]] = []
        self.patches = [
            patch("app.services.pasarguard.get_pg", return_value=self.pg),
            patch("app.services.billing_suspend._set_admin_enabled", AsyncMock()),
            patch("app.services.pg_admin_subscription.assert_child_growth_fits", AsyncMock()),
            patch("app.services.reseller_capacity.assert_child_growth_fits", AsyncMock()),
        ]
        for p in self.patches:
            p.start()

        def render(request: Request, template: str, ctx: dict):
            self.rendered.append((template, ctx))
            return HTMLResponse("ok")

        async def require_staff():
            return dict(self.staff)

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_reseller_subscription_pages(app, render=render, require_staff=require_staff, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        await self.engine.dispose()

    async def wallet(self, user_id: int = 3) -> int:
        async with self.Session() as session:
            return int((await session.get(BotUser, user_id)).wallet_balance)

    async def sub(self, pg_username: str = "shop3") -> PgAdminSubscription:
        async with self.Session() as session:
            return await session.scalar(select(PgAdminSubscription).where(PgAdminSubscription.pg_username == pg_username))

    async def post(self, path: str, **data) -> dict[str, list[str]]:
        response = await self.client.post(f"/my-subscription{path}", data={k: str(v) for k, v in data.items()})
        self.assertEqual(response.status_code, 303, response.text)
        return parse_qs(urlsplit(response.headers["location"]).query)

    async def test_page_shows_the_owners_packages(self) -> None:
        self.assertEqual((await self.client.get("/my-subscription")).status_code, 200)
        template, ctx = self.rendered[-1]
        self.assertEqual(template, "my_subscription.html")
        self.assertEqual(ctx["overview"].generation, 2)
        self.assertEqual(ctx["wallet"], 1_000_000)
        self.assertEqual([int(c.plan.id) for c in ctx["overview"].choices][:2], [1, 2])
        self.assertIsNotNone(ctx["extras"])

    async def test_only_the_shop_owner_reaches_it(self) -> None:
        for staff in ({"role": "principal", "bot_user_id": 3}, {"role": "admin"}, {"role": "reseller"}):
            with self.subTest(staff=staff):
                self.staff = staff
                self.assertEqual((await self.client.get("/my-subscription")).status_code, 403)
                self.assertEqual((await self.client.post("/my-subscription/renew", data={"plan_id": "2", "generation": "2"})).status_code, 403)
        self.assertEqual(await self.wallet(), 1_000_000)

    async def test_one_renewal_per_submitted_page(self) -> None:
        self.assertIn("ok", await self.post("/renew", plan_id=2, generation=2))
        self.assertEqual(await self.wallet(), 800_000)
        again = await self.post("/renew", plan_id=2, generation=2)
        self.assertEqual(again["err"], [STALE_PAGE])
        self.assertEqual(await self.wallet(), 800_000)
        self.assertEqual((await self.sub()).plan_id, 2)
        await self.client.get("/my-subscription")
        self.assertTrue(self.rendered[-1][1]["overview"].expires_at_iso.endswith("+00:00"), "expiry is shown as UTC")

    async def test_renewal_touches_only_the_callers_shop(self) -> None:
        await self.post("/renew", plan_id=1, generation=2)
        other = await self.sub("shop4")
        self.assertEqual((other.renew_generation, await self.wallet(4)), (7, 1_000_000))
        self.assertEqual(self.pg.modify_admin.await_args.args[0], "shop3")

    async def test_refused_packages_and_bad_input_charge_nothing(self) -> None:
        for data in ({"plan_id": 3, "generation": 2}, {"plan_id": 0, "generation": 2}, {"plan_id": 999, "generation": 2}):
            with self.subTest(data=data):
                self.assertIn("err", await self.post("/renew", **data))
        bad = await self.client.post("/my-subscription/renew", data={"plan_id": "x", "generation": "2"})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(await self.wallet(), 1_000_000)

    async def test_extras_use_the_allowed_amounts(self) -> None:
        self.assertIn("ok", await self.post("/extra", kind="gb", amount=5))
        self.assertEqual(await self.wallet(), 995_000)
        for data in ({"kind": "gb", "amount": 7}, {"kind": "users", "amount": 3}, {"kind": "root", "amount": 5}):
            with self.subTest(data=data):
                self.assertIn("err", await self.post("/extra", **data))
        self.assertEqual(await self.wallet(), 995_000)


if __name__ == "__main__":
    unittest.main()
