"""Finance reports tab: orders and new users per Tehran day, scoped to one shop, plus method shares."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, Order
from app.services.dashboard_trend import TEHRAN
from app.services.finance_charts import finance_charts, method_segments
from tests.test_update_owner_only import OWNER_SESSION, RoutesHarness

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=TEHRAN)
SHOP = 5


def at(days_ago: int, hour: int = 12) -> datetime:
    day = (NOW - timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return day.astimezone(timezone.utc)


def order(user_id: int, when: datetime, *, status: str = "delivered", shop: int | None = None) -> Order:
    return Order(user_id=user_id, amount=1000, discount_amount=0, quantity=1, status=status, reseller_id=shop, created_at=when)


class DailyChartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'c.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=101, referral_code="a", role="user", created_at=at(0)),
                BotUser(id=2, telegram_id=102, referral_code="b", role="user", is_demo=True, created_at=at(0)),
                BotUser(id=3, telegram_id=103, referral_code="c", role="user", created_at=at(2)),
                BotUser(id=SHOP, telegram_id=105, referral_code="s", role="reseller", created_at=at(40)),
                BotUser(id=6, telegram_id=106, referral_code="d", role="user", reseller_id=SHOP, created_at=at(0)),
            ])
            await session.flush()
            session.add_all([
                order(1, at(0)), order(1, at(0, 1)), order(1, at(0), status="pending"),
                order(1, at(1), status="cancelled"),
                order(1, at(9)),
                order(2, at(0)),
                order(6, at(0), shop=SHOP), order(6, at(0), shop=SHOP),
            ])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def charts(self, **kw):
        async with self.Session() as session:
            return await finance_charts(session, now=NOW, **kw)

    async def test_platform_days_count_orders_with_delivered_and_real_new_users(self) -> None:
        charts = await self.charts(reseller_id=None)
        week = charts["orders"][7]
        days = week["days"]
        self.assertEqual(len(days), 7)
        self.assertEqual((days[-1]["value"], days[-1]["extra"]), (3, 2), "today: 3 orders, 2 delivered")
        self.assertEqual((days[-2]["value"], days[-2]["extra"]), (1, 0))
        self.assertEqual(days[-1]["date"], "2026-10-17")
        self.assertEqual(week["total"], 4, "demo orders are out")
        users = charts["new_users"][7]
        self.assertEqual([d["value"] for d in users["days"]][-3:], [1, 0, 1], "demo users and shop customers are out")

    async def test_each_range_is_compared_with_the_period_before(self) -> None:
        charts = await self.charts(reseller_id=None)
        week, month = charts["orders"][7], charts["orders"][30]
        self.assertEqual((week["previous_total"], week["change_pct"]), (1, 300), "the order 9 days ago is last week")
        self.assertEqual((len(month["days"]), month["total"], month["previous_total"], month["change_pct"]), (30, 5, 0, None))
        self.assertTrue(week["has_data"])

    async def test_a_shop_sees_only_its_own_orders_and_customers(self) -> None:
        charts = await self.charts(reseller_id=SHOP)
        self.assertEqual((charts["orders"][7]["total"], charts["new_users"][7]["total"]), (2, 1))
        self.assertEqual(charts["orders"][7]["days"][-1]["extra"], 2)
        empty = await self.charts(reseller_id=99)
        self.assertFalse(empty["orders"][30]["has_data"] or empty["new_users"][30]["has_data"])

    async def test_a_failed_query_gives_empty_charts(self) -> None:
        with patch("app.services.finance_charts._daily_counts", side_effect=OperationalError("select", {}, Exception("x"))):
            charts = await self.charts(reseller_id=None)
        self.assertEqual((len(charts["orders"][7]["days"]), charts["orders"][7]["has_data"]), (7, False))
        self.assertEqual(len(charts["new_users"][30]["days"]), 30)


class MethodSegmentTests(unittest.TestCase):
    def test_small_methods_are_grouped_and_empty_ones_dropped(self) -> None:
        rows = [{"label": f"m{i}", "amount": 100 - i, "share_pct": 15} for i in range(6)]
        rows.append({"label": "zero", "amount": 0, "share_pct": 0})
        segments = method_segments(rows)
        self.assertEqual([s["label"] for s in segments], ["m0", "m1", "m2", "m3", "سایر"])
        self.assertEqual((segments[-1]["share_pct"], segments[-1]["tone"]), (30, 5))

    def test_a_single_extra_method_is_not_called_other(self) -> None:
        rows = [{"label": f"m{i}", "amount": 10, "share_pct": 20} for i in range(5)]
        self.assertEqual([s["label"] for s in method_segments(rows)], ["m0", "m1", "m2", "m3", "m4"])


class ReportsTabChartTests(RoutesHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        Session = async_sessionmaker(self.engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        async with Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=101, referral_code="u1", role="user", created_at=now),
                BotUser(id=6, telegram_id=106, referral_code="u6", role="user", reseller_id=SHOP, created_at=now),
            ])
            await session.flush()
            session.add_all([
                Order(user_id=1, amount=30000, status="delivered", payment_method="card", created_at=now),
                Order(user_id=1, amount=10000, status="delivered", payment_method="wallet", created_at=now),
                Order(user_id=6, reseller_id=SHOP, amount=50000, status="delivered", payment_method="card", created_at=now),
            ])
            await session.commit()

    async def test_the_reports_tab_draws_daily_charts_and_the_share_bar(self) -> None:
        cookie = f"session={self.signer.dumps(OWNER_SESSION)}; csrf=token"
        page = await self.client.get("/finance?tab=reports", headers={"cookie": cookie})
        self.assertEqual(page.status_code, 200)
        cards = re.findall(r"<section[^>]*data-metric=\"value\"[^>]*>", page.text)
        self.assertEqual(len(cards), 2, "orders and new users use the dashboard chart")
        orders = json.loads(unescape(re.search(r"data-trend='([^']*)'", cards[0]).group(1)))
        self.assertEqual(orders["total"], 2, "the shop's order is not in the platform chart")
        self.assertIn("data-trend-30=", cards[0])
        self.assertIn('data-extra="extra"', cards[0])
        self.assertIn('class="fin-share"', page.text)
        self.assertEqual(page.text.count('class="fin-share-seg"'), 2)


if __name__ == "__main__":
    unittest.main()
