"""Bot and PasarGuard overviews: period KPIs, role-scoped views, bot status cache, rendered cards."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock


from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.db.models import BotUser, Order
from app.services import bot_overview
from app.services.bot_overview import (
    PeriodFigures, PeriodKpis, Viewer, change_pct, parse_period, period_kpis, viewer_of,
)
from app.services.pg_overview_view import overview_view, owned_user_counts, owner_user_counts, segments
from tests.postgres_support import scoped_async_engine

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)
SHOP = 5
OTHER_SHOP = 6
OWNER = {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_parent_id": None}


def order(user_id: int, amount: int, days_ago: int, *, shop: int | None = None, status: str = "delivered") -> Order:
    return Order(
        user_id=user_id, amount=amount, discount_amount=0, quantity=1, status=status,
        reseller_id=shop, created_at=NOW - timedelta(days=days_ago),
    )


class PeriodKpiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=101, referral_code="a", role="user", created_at=NOW),
                BotUser(id=2, telegram_id=102, referral_code="b", role="user", is_demo=True, created_at=NOW),
                BotUser(id=3, telegram_id=103, referral_code="c", role="user", created_at=NOW - timedelta(days=9)),
                BotUser(id=4, telegram_id=104, referral_code="d", role="user", reseller_id=SHOP, created_at=NOW),
                BotUser(id=SHOP, telegram_id=105, referral_code="s", role="reseller", created_at=NOW - timedelta(days=90)),
                BotUser(id=OTHER_SHOP, telegram_id=106, referral_code="o", role="reseller", created_at=NOW - timedelta(days=90)),
            ])
            await session.flush()
            session.add_all([
                order(1, 1000, 0), order(1, 500, 2),
                order(1, 600, 9),
                order(2, 9000, 0),
                order(1, 4000, 0, status="awaiting_approval"),
                order(4, 700, 1, shop=SHOP), order(4, 300, 8, shop=SHOP),
                order(4, 8000, 0, shop=OTHER_SHOP),
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def kpis(self, shop: int | None, period: str = "week") -> PeriodKpis:
        async with self.Session() as session:
            return await period_kpis(session, reseller_id=shop, period=period, now=NOW)

    async def test_platform_week_counts_delivered_real_orders_against_the_week_before(self):
        result = await self.kpis(None)
        self.assertEqual(result.current, PeriodFigures(revenue=1500, delivered=2, new_users=1))
        self.assertEqual(result.previous, PeriodFigures(revenue=600, delivered=1, new_users=1))
        self.assertEqual(result.change_pct("revenue"), 150)

    async def test_shop_sees_only_its_own_orders_and_customers(self):
        result = await self.kpis(SHOP)
        self.assertEqual(result.current, PeriodFigures(revenue=700, delivered=1, new_users=1))
        self.assertEqual(result.previous.revenue, 300)
        other = await self.kpis(OTHER_SHOP)
        self.assertEqual(other.current.revenue, 8000)
        self.assertEqual(other.current.new_users, 0)

    async def test_unknown_period_falls_back_to_week(self):
        self.assertEqual((await self.kpis(None, "year")).period, "week")


class PeriodHelperTests(unittest.TestCase):
    def test_change_pct_needs_a_previous_value(self):
        self.assertIsNone(change_pct(10, 0))
        self.assertEqual(change_pct(50, 100), -50)

    def test_parse_period_accepts_only_known_names(self):
        self.assertEqual(parse_period("DAY"), "day")
        self.assertEqual(parse_period("<script>"), "week")
        self.assertEqual(parse_period(None), "week")


class ViewerTests(unittest.TestCase):
    def test_platform_owner_gets_the_platform_shop_with_every_link(self):
        viewer = viewer_of(OWNER)
        self.assertEqual((viewer.shop_id, viewer.has_shop, viewer.can_payments), (None, True, True))

    def test_shop_staff_links_follow_their_permissions(self):
        viewer = viewer_of({"role": "reseller", "bot_user_id": SHOP, "permissions": ["dashboard", "orders"]})
        self.assertEqual(viewer.shop_id, SHOP)
        self.assertTrue(viewer.can_orders)
        self.assertFalse(viewer.can_payments)
        self.assertFalse(viewer.can_tickets)
        self.assertFalse(viewer.can_settings)

    def test_bare_admin_without_owner_principal_gets_no_shop(self):
        for staff in ({"role": "admin", "permissions": ["payments"]}, {"role": "pg_staff"}, {}):
            viewer = viewer_of(staff)
            self.assertFalse(viewer.has_shop)
            self.assertFalse(viewer.can_payments)


class BotStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot_overview._bot_status_cache.clear()

    async def test_missing_token_is_reported_without_a_probe(self):
        with mock.patch.object(bot_overview, "check_bot_connection") as probe:
            status = await bot_overview.bot_status("  ")
        probe.assert_not_called()
        self.assertEqual(status["state"], "err")

    async def test_probe_is_cached_under_a_hash_not_the_token(self):
        token = "123456:SECRET-token"
        probe = mock.AsyncMock(return_value={"ok": True, "username": "shopbot"})
        with mock.patch.object(bot_overview, "check_bot_connection", probe):
            first = await bot_overview.bot_status(token)
            second = await bot_overview.bot_status(token)
        self.assertEqual(probe.await_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(first["username"], "shopbot")
        self.assertTrue(all(token not in key for key in bot_overview._bot_status_cache))

    async def test_slow_probe_reads_unchecked_and_is_not_cached(self):
        async def slow(_token):
            await asyncio.sleep(1)
            return {"ok": True}

        with mock.patch.object(bot_overview, "check_bot_connection", slow), \
                mock.patch.object(bot_overview, "BOT_STATUS_TIMEOUT_SECONDS", 0.01):
            status = await bot_overview.bot_status("1:abc")
        self.assertEqual(status["state"], "neutral")
        self.assertEqual(len(bot_overview._bot_status_cache), 0)


class PgOverviewViewTests(unittest.TestCase):
    STATS = {"total_user": 10, "online_users": 3, "active_users": 6, "expired_users": 2,
             "limited_users": True, "disabled_users": 2, "version": "1.2.0"}
    NODES = [{"status_kind": "ok"}, {"status_kind": "err"}]

    def test_owner_sees_panel_counts_version_and_nodes(self):
        view = overview_view(owner=True, system_stats=self.STATS, user_stats=None, nodes=self.NODES)
        self.assertEqual(view["counts"].total, 10)
        self.assertEqual(view["counts"].limited, 0, "a bool is not a count")
        self.assertEqual(view["version"], "1.2.0")
        self.assertEqual((view["nodes_connected"], view["nodes_total"], view["nodes_state"]), (1, 2, "warn"))

    def test_reseller_never_gets_panel_wide_figures(self):
        own = {"total": 4, "online": 1, "active": 3, "expired": 1}
        view = overview_view(owner=False, system_stats=self.STATS, user_stats=own, nodes=self.NODES)
        self.assertEqual(view["counts"].total, 4)
        self.assertIsNone(view["version"])
        self.assertEqual(view["nodes_total"], 0)

    def test_counts_read_older_key_names_and_reject_negatives(self):
        counts = owner_user_counts({"users_total": 7, "users_online": -1})
        self.assertEqual((counts.total, counts.online), (7, 0))
        self.assertEqual(owned_user_counts(None).total, 0)

    def test_segments_split_users_by_status(self):
        parts = segments(owned_user_counts({"active": 3, "expired": 1}))
        self.assertEqual([(s.key, s.pct) for s in parts], [("active", 75.0), ("expired", 25.0)])
        self.assertEqual(segments(owned_user_counts({})), [])


def _render(template: str, **context) -> str:
    from app.api.app import templates

    base = {
        "request": SimpleNamespace(url=SimpleNamespace(path="/pg/body"), state=SimpleNamespace(), query_params={}),
        "pg_perms": [], "counts": {"admins": 1, "groups": 2, "hosts": 3, "nodes": 1}, "flash_err": None,
    }
    base.update(context)
    return templates.env.get_template(template).render(base)


HOST = {"cpu_percent": 41.0, "memory_percent": 63.0, "cpu_cores": 4, "cpu_tone": "ok", "mem_tone": "warn"}
NODE = {"id": 7, "name": "<img src=x onerror=alert(1)>", "status": "connected", "status_kind": "ok",
        "cpu_percent": 10, "mem_percent": 20, "cpu_text": "10%", "mem_text": "20%"}


class PgBodyRenderTests(unittest.TestCase):
    def test_owner_body_shows_server_traffic_users_then_nodes(self):
        view = overview_view(owner=True, system_stats={"total_user": 9}, user_stats=None, nodes=[NODE])
        html = _render(
            "_pg_dash_body.html", staff={"pg_is_owner": True}, view=view, host_gauges=HOST,
            nodes_overview={"nodes": [NODE], "live": {"rate_up_num": "1.2", "rate_up_unit": "MB/s"}},
            reseller_overview=None,
        )
        order_of = [html.index(mark) for mark in ("سرور پاسارگارد", "ترافیک زنده", "کل کاربران", "کاربران و اجزای پنل", 'data-node-id="7"')]
        self.assertEqual(order_of, sorted(order_of))
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img src=x", html)

    def test_reseller_body_has_no_server_nodes_or_panel_counts(self):
        own = {"total": 4, "online": 1, "active": 3}
        view = overview_view(owner=False, system_stats=None, user_stats=own, nodes=None)
        html = _render(
            "_pg_dash_body.html", staff={"pg_is_owner": False}, view=view, host_gauges=HOST,
            nodes_overview={"nodes": [NODE], "live": {}},
            reseller_overview={"ready": True, "user_stats": own},
        )
        self.assertIn("کاربران من", html)
        for leak in ("سرور پاسارگارد", "ترافیک زنده", "data-node-id", "/pg/metrics", "اجزای پنل"):
            self.assertNotIn(leak, html)


class DashboardRenderTests(unittest.TestCase):
    def context(self, viewer: Viewer) -> dict:
        empty = PeriodFigures(revenue=0, delivered=0, new_users=0)
        return {
            "request": SimpleNamespace(url=SimpleNamespace(path="/dashboard"), state=SimpleNamespace(), query_params={}),
            "staff": {"role": "reseller", "permissions": []}, "viewer": viewer, "period": "week",
            "period_labels": bot_overview.PERIOD_LABELS, "compare_label": "نسبت به ۷ روز قبل",
            "host_gauges": None, "bot_setup_needed": False,
            "stats": {"pending": 2, "tickets": 1, "services": 0, "users": 0, "orders": 0, "revenue": 0},
            "kpis": PeriodKpis(period="week", current=empty, previous=empty), "deliveries": 0,
            "trend": {"days": [], "has_data": False}, "recent_payments": [], "recent_orders": [],
            "bot": {"state": "ok", "username": "shopbot", "detail": "آنلاین"},
        }

    def test_shop_staff_without_finance_permissions_get_no_rows_links_or_server(self):
        viewer = Viewer(shop_id=SHOP, has_shop=True, can_payments=False, can_orders=False, can_tickets=False,
                        can_settings=False)
        html = _render("dashboard.html", **self.context(viewer))
        for hidden in ("آخرین پرداخت‌ها", "آخرین سفارش‌ها", 'href="/finance', 'href="/tickets"', "سرور ربات",
                       "/shop-settings"):
            self.assertNotIn(hidden, html)
        self.assertIn("رسید منتظر تأیید", html)

    def test_waiting_order_is_tagged_waiting(self):
        viewer = Viewer(shop_id=SHOP, has_shop=True, can_payments=True, can_orders=True, can_tickets=True,
                        can_settings=True)
        ctx = self.context(viewer)
        ctx["recent_orders"] = [SimpleNamespace(id=3, amount=1000, status="awaiting_approval")]
        html = _render("dashboard.html", **ctx)
        self.assertIn("در انتظار", html)
        self.assertNotIn("در انتظار پرداخت", html)
        self.assertIn('href="/shop-settings"', html)


class MetricsRouteTests(unittest.TestCase):
    def test_bot_server_metrics_are_owner_only(self):
        from app.api.bot_overview_pages import register_bot_overview_pages

        def deny():
            raise HTTPException(status_code=403, detail="forbidden")

        app = FastAPI()
        register_bot_overview_pages(
            app, render=None, require_perm=lambda _key: (lambda: {}), require_admin=deny, get_db=lambda: None,
        )
        response = TestClient(app).get("/dashboard/metrics")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("cpu_percent", response.text)


if __name__ == "__main__":
    unittest.main()
