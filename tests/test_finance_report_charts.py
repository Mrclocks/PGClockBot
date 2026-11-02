"""The finance «گزارشات» tab shows the sales chart and each payment method's share of sales."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import BotUser, Order
from app.services.dashboard_trend import clear_trend_cache, sales_trend_or_empty
from app.services.finance_reports import shop_payment_method_breakdown
from tests.test_update_owner_only import OWNER_SESSION, RoutesHarness

NOW = datetime.now(timezone.utc)


class FinanceChartCase(RoutesHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        clear_trend_cache()
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=101, referral_code="u1", role="user"),
                BotUser(id=6, telegram_id=106, referral_code="u6", role="user", reseller_id=5),
            ])
            await session.flush()
            session.add_all([
                Order(user_id=1, amount=30000, status="delivered", payment_method="card", created_at=NOW),
                Order(user_id=1, amount=10000, status="delivered", payment_method="wallet", created_at=NOW),
                Order(user_id=1, amount=99999, status="pending", payment_method="card", created_at=NOW),
                Order(user_id=6, reseller_id=5, amount=50000, status="delivered", payment_method="card", created_at=NOW),
            ])
            await session.commit()


class MethodShareTests(FinanceChartCase):
    async def test_each_method_gets_its_share_of_delivered_sales_in_its_own_shop(self):
        async with self.Session() as session:
            methods = await shop_payment_method_breakdown(session, reseller_id=None, since=NOW - timedelta(days=1))
        shares = {m["method"]: (m["amount"], m["share_pct"]) for m in methods}
        self.assertEqual(shares, {"card": (30000, 75), "wallet": (10000, 25)})

    async def test_no_sales_means_no_share(self):
        async with self.Session() as session:
            self.assertEqual(await shop_payment_method_breakdown(session, reseller_id=99, since=NOW), [])


class TrendFallbackTests(FinanceChartCase):
    async def test_a_failed_query_gives_an_empty_chart(self):
        async with self.Session() as session:
            with patch(
                "app.services.dashboard_trend.sales_trend",
                side_effect=OperationalError("select", {}, Exception("locked")),
            ):
                trend = await sales_trend_or_empty(session, reseller_id=None)
        self.assertFalse(trend["has_data"])
        self.assertEqual(len(trend["days"]), 7)


class ReportsTabTests(FinanceChartCase):
    async def test_the_reports_tab_draws_the_sales_chart_with_this_scope(self):
        cookie = f"session={self.signer.dumps(OWNER_SESSION)}; csrf=token"
        page = await self.client.get("/finance?tab=reports", headers={"cookie": cookie})
        self.assertEqual(page.status_code, 200)
        self.assertIn("data-sales-chart", page.text)
        self.assertIn("/static/sales-chart.js", page.text)
        trend = json.loads(unescape(re.search(r"data-trend='([^']*)'", page.text).group(1)))
        # Platform view: only platform orders, never the shop's 50,000.
        self.assertEqual(trend["total"], 40000)
        self.assertIn("سهم از فروش", page.text)
        self.assertIn("--p: 75", page.text)
