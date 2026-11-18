"""Dashboard sales trend: Tehran days, shop scope, demo exclusion and comparison with the previous period."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone


from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.db.models import BotUser, Order, UserService
from app.services.dashboard_trend import (
    TEHRAN, change_percent, empty_trend, own_account_summary, sales_trend,
)
from tests.postgres_support import scoped_async_engine

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=TEHRAN)
SHOP = 5


def at(days_ago: int, hour: int = 12) -> datetime:
    day = (NOW - timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return day.astimezone(timezone.utc)


def order(user_id: int, amount: int, when: datetime, *, shop: int | None = None, status: str = "delivered") -> Order:
    return Order(user_id=user_id, amount=amount, discount_amount=0, quantity=1, status=status, reseller_id=shop, created_at=when)


class TrendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=101, referral_code="a", role="user"),
                BotUser(id=2, telegram_id=102, referral_code="b", role="user", is_demo=True),
                BotUser(id=SHOP, telegram_id=105, referral_code="s", role="reseller"),
            ])
            await session.flush()
            session.add_all([
                order(1, 1000, at(0)), order(1, 500, at(0, 1)), order(1, 2000, at(1)),
                order(1, 700, at(8)),
                order(1, 9999, at(0), shop=SHOP),
                order(2, 8888, at(0)),
                order(1, 4444, at(0), status="awaiting_approval"),
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def trend(self, **kw):
        async with self.Session() as session:
            return await sales_trend(session, now=NOW, **kw)

    async def test_platform_trend_counts_only_delivered_platform_orders_of_real_customers(self):
        result = await self.trend(reseller_id=None)
        self.assertEqual([d["revenue"] for d in result["days"]], [0, 0, 0, 0, 0, 2000, 1500])
        self.assertEqual(result["days"][-1]["orders"], 2)
        self.assertEqual((result["today"], result["yesterday"]), (1500, 2000))
        self.assertEqual(result["today_change_pct"], -25)
        self.assertEqual(result["total"], 3500)
        self.assertEqual(result["previous_total"], 700)
        self.assertEqual(result["change_pct"], 400)
        self.assertTrue(result["has_data"])

    async def test_a_shop_sees_only_its_own_orders(self):
        result = await self.trend(reseller_id=SHOP)
        self.assertEqual(result["total"], 9999)
        other = await self.trend(reseller_id=77)
        self.assertEqual(other["total"], 0)
        self.assertFalse(other["has_data"])

    async def test_days_follow_tehran_midnight(self):
        async with self.Session() as session:
            session.add(order(1, 300, (NOW.replace(hour=0, minute=30)).astimezone(timezone.utc)))
            session.add(order(1, 100, (NOW.replace(hour=0, minute=0) - timedelta(minutes=10)).astimezone(timezone.utc)))
            await session.commit()
        result = await self.trend(reseller_id=None)
        self.assertEqual(result["days"][-1]["revenue"], 1800)
        self.assertEqual(result["days"][-2]["revenue"], 2100)

    async def test_thirty_day_window_has_thirty_points_and_other_lengths_are_refused(self):
        self.assertEqual(len((await self.trend(reseller_id=None, days=30))["days"]), 30)
        with self.assertRaises(ValueError):
            await self.trend(reseller_id=None, days=14)

    def test_change_percent_needs_a_previous_value(self):
        self.assertIsNone(change_percent(10, 0))
        self.assertEqual(change_percent(150, 100), 50)

    def test_empty_trend_has_a_label_for_every_day(self):
        self.assertEqual(len(empty_trend(7)["days"]), 7)
        self.assertFalse(empty_trend(7)["has_data"])


class AccountSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_counts_active_platform_services_and_nearest_expiry(self):
        engine = scoped_async_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime(2026, 10, 17, tzinfo=timezone.utc)
        async with Session() as session:
            session.add(BotUser(id=1, telegram_id=1, referral_code="a", role="admin", wallet_balance=1200))
            session.add(BotUser(id=5, telegram_id=5, referral_code="shop5", role="reseller"))
            await session.flush()
            session.add_all([
                UserService(bot_user_id=1, pg_user_id=1, pg_username="a", quota_expire_at=now + timedelta(days=9, hours=2)),
                UserService(bot_user_id=1, pg_user_id=2, pg_username="b", quota_expire_at=now + timedelta(days=20)),
                UserService(bot_user_id=1, pg_user_id=3, pg_username="c", is_cancelled=True),
                UserService(bot_user_id=1, pg_user_id=4, pg_username="d", shop_id=5),
            ])
            await session.commit()
            summary = await own_account_summary(session, 1, now=now)
        await engine.dispose()
        self.assertEqual(summary, {"wallet": 1200, "services": 2, "nearest_expiry_days": 9})

    async def test_expired_or_blocked_services_are_not_counted_as_active(self):
        engine = scoped_async_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime(2026, 10, 17, tzinfo=timezone.utc)
        async with Session() as session:
            session.add(BotUser(id=1, telegram_id=1, referral_code="a", role="admin"))
            await session.flush()
            session.add_all([
                UserService(bot_user_id=1, pg_user_id=1, pg_username="past", quota_expire_at=now - timedelta(days=3)),
                UserService(bot_user_id=1, pg_user_id=2, pg_username="exp", quota_status="expired",
                            quota_expire_at=now + timedelta(days=4)),
                UserService(bot_user_id=1, pg_user_id=3, pg_username="off", quota_status="disabled"),
                UserService(bot_user_id=1, pg_user_id=4, pg_username="lim", quota_status="limited"),
                UserService(bot_user_id=1, pg_user_id=5, pg_username="ok", quota_status="active",
                            quota_expire_at=now + timedelta(days=12)),
                UserService(bot_user_id=1, pg_user_id=6, pg_username="forever"),
            ])
            await session.commit()
            summary = await own_account_summary(session, 1, now=now)
        await engine.dispose()
        self.assertEqual(summary["services"], 2)
        self.assertEqual(summary["nearest_expiry_days"], 12)


class TrendRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        from fastapi import FastAPI

        from app.api.dashboard_trend_pages import register_dashboard_trend_pages

        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([BotUser(id=1, telegram_id=1, referral_code="a", role="user"),
                             BotUser(id=SHOP, telegram_id=5, referral_code="s", role="reseller")])
            await session.flush()
            session.add_all([order(1, 100, datetime.now(timezone.utc)), order(1, 900, datetime.now(timezone.utc), shop=SHOP)])
            await session.commit()
        self.staff = {"role": "reseller", "bot_user_id": SHOP}
        app = FastAPI()

        async def require_staff():
            return self.staff

        async def get_db():
            async with self.Session() as session:
                yield session

        register_dashboard_trend_pages(app, require_staff=require_staff, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        from app.services.dashboard_trend import clear_trend_cache

        clear_trend_cache()

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()

    async def test_a_shop_staff_gets_only_its_own_numbers(self):
        response = await self.client.get("/home/sales-trend?days=7")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 900)

    async def test_week_figures_are_always_fresh_and_the_month_view_is_cached(self):
        self.assertEqual((await self.client.get("/home/sales-trend?days=7")).json()["total"], 900)
        month = (await self.client.get("/home/sales-trend?days=30")).json()["total"]
        async with self.Session() as session:
            session.add(order(1, 50, datetime.now(timezone.utc), shop=SHOP))
            await session.commit()
        self.assertEqual((await self.client.get("/home/sales-trend?days=7")).json()["total"], 950)
        self.assertEqual((await self.client.get("/home/sales-trend?days=30")).json()["total"], month)

    async def test_unsupported_ranges_and_staff_without_a_shop_are_refused(self):
        self.assertEqual((await self.client.get("/home/sales-trend?days=3")).status_code, 400)
        self.staff = {"role": "pg_staff"}
        self.assertEqual((await self.client.get("/home/sales-trend?days=7")).status_code, 403)
