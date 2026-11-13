"""Reseller screen: capacity text (unlimited vs a number), live comparison and the manual re-sync."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import PgAdminSubscription, ResellerPlan, ResellerProfile
from app.services.pg_admin_subscription import PgCapacityUnreadable
from app.services.reseller_capacity_view import (
    LiveCapacity,
    build_capacity_view,
    live_capacity_from_overview,
    sync_reseller_capacity,
)
from app.services.resellers import ensure_current_capacity_reseller_plan
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

GB = 1024**3


def sub_of(base_gb=0, base_users=0, extra_gb=0, extra_users=0):
    return SimpleNamespace(
        base_gb=base_gb, base_users=base_users, extra_gb_purchased=extra_gb, extra_users_purchased=extra_users
    )


class ViewTests(unittest.TestCase):
    def test_zero_is_unlimited_and_numbers_are_numbers(self):
        view = build_capacity_view(sub_of(0, 0), current_capacity=True, live=None)
        self.assertEqual((view["total_gb_text"], view["total_users_text"]), ("نامحدود", "نامحدود"))
        view = build_capacity_view(sub_of(500, 50, 20, 5), current_capacity=False, live=None)
        self.assertEqual((view["total_gb_text"], view["total_users_text"]), ("520 گیگ", "55 کاربر"))
        self.assertEqual((view["extra_gb_text"], view["extra_users_text"]), ("20 گیگ", "5 کاربر"))

    def test_an_unread_live_limit_is_not_shown_as_unlimited(self):
        view = build_capacity_view(sub_of(500, 50), current_capacity=True, live=None)
        self.assertEqual((view["live_gb_text"], view["live_known"], view["differs"]), ("خوانده نشد", False, False))

    def test_a_difference_from_the_live_limits_is_flagged_only_for_current_capacity(self):
        live = LiveCapacity(gb=500, users=50)
        self.assertTrue(build_capacity_view(sub_of(0, 0), current_capacity=True, live=live)["differs"])
        self.assertFalse(build_capacity_view(sub_of(500, 50), current_capacity=True, live=live)["differs"])
        other = build_capacity_view(sub_of(0, 0), current_capacity=False, live=live)
        self.assertEqual((other["differs"], other["can_sync"]), (False, False))

    def test_live_limits_come_from_the_overview(self):
        overview = {"ready": True, "traffic": {"limit": 500 * GB}, "users": {"limit": 50}}
        self.assertEqual(live_capacity_from_overview(overview), LiveCapacity(500, 50))
        unlimited = {"ready": True, "traffic": {"limit": None}, "users": {"limit": None}}
        self.assertEqual(live_capacity_from_overview(unlimited), LiveCapacity(0, 0))
        self.assertIsNone(live_capacity_from_overview({"ready": False}))
        self.assertIsNone(live_capacity_from_overview(None))
        self.assertEqual(live_capacity_from_overview({"ready": True, "traffic": {"limit": GB // 2}}).gb, 1)


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'v.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            self.cc_id = (await ensure_current_capacity_reseller_plan(session)).id
            fixed = ResellerPlan(name="ثابت", price=0, plan_kind="subscription")
            session.add(fixed)
            await session.flush()
            self.fixed_id = fixed.id
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmp.cleanup()

    async def add_sub(self, plan_id, base=(0, 0), extra=(0, 0)):
        async with self.Session() as session:
            session.add(PgAdminSubscription(
                pg_username="shopadmin", plan_id=plan_id, base_gb=base[0], base_users=base[1],
                extra_gb_purchased=extra[0], extra_users_purchased=extra[1],
            ))
            await session.commit()

    async def sync(self, live):
        profile = SimpleNamespace(pg_admin_username="shopadmin")
        async with self.Session() as session:
            with patch("app.services.reseller_capacity_view.read_live_pg_admin_capacity", live):
                return await sync_reseller_capacity(session, profile)

    async def stored(self):
        async with self.Session() as session:
            row = (await session.scalars(select(PgAdminSubscription))).one()
            return row.base_gb, row.base_users, row.extra_gb_purchased, row.extra_users_purchased

    async def test_the_base_follows_the_live_limits_and_purchased_extras_are_kept(self):
        await self.add_sub(self.cc_id, base=(0, 0), extra=(20, 5))
        self.assertEqual(await self.sync(AsyncMock(return_value=(500, 50))), (500, 50))
        # Live already includes extras; base stores only the residual so the sales ceiling is 500.
        self.assertEqual(await self.stored(), (480, 45, 20, 5))

    async def test_a_panel_that_cannot_be_read_changes_nothing(self):
        await self.add_sub(self.cc_id, base=(10, 2))
        with self.assertRaises(PgCapacityUnreadable):
            await self.sync(AsyncMock(side_effect=PgCapacityUnreadable("x")))
        self.assertEqual(await self.stored(), (10, 2, 0, 0))

    async def test_other_plans_and_missing_subscriptions_are_refused(self):
        with self.assertRaises(ValueError):
            await self.sync(AsyncMock(return_value=(1, 1)))
        await self.add_sub(self.fixed_id, base=(10, 2))
        with self.assertRaises(ValueError):
            await self.sync(AsyncMock(return_value=(500, 50)))
        self.assertEqual(await self.stored(), (10, 2, 0, 0))


class ScreenTests(RoutesHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            plan = await ensure_current_capacity_reseller_plan(session)
            await session.execute(update(ResellerProfile).where(ResellerProfile.user_id == 5).values(pg_admin_username="shopadmin"))
            session.add(PgAdminSubscription(pg_username="shopadmin", plan_id=plan.id, base_gb=0, base_users=0))
            await session.commit()

    async def call_form(self, payload, method, path):
        cookie = f"session={self.signer.dumps(payload)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.request(method, path, headers=headers, data={} if method == "POST" else None)

    async def test_the_edit_screen_shows_users_unlimited_and_the_sync_button(self):
        overview = {"ready": True, "traffic": {"limit": 500 * GB}, "users": {"limit": 50}}
        with patch("app.services.pg_overview.build_reseller_pg_overview", AsyncMock(return_value=overview)):
            response = await self.call_form(OWNER_SESSION, "GET", "/resellers/5/edit?fragment=1")
        self.assertEqual(response.status_code, 200)
        for needle in ("کاربران کل", "نامحدود", "/resellers/5/subscription/sync", "500 گیگ", "یکی نیست"):
            self.assertIn(needle, response.text)

    async def test_the_owner_syncs_and_a_shop_cannot(self):
        with patch("app.services.reseller_capacity_view.read_live_pg_admin_capacity", AsyncMock(return_value=(500, 50))):
            ok = await self.call_form(OWNER_SESSION, "POST", "/resellers/5/subscription/sync")
            denied = await self.call_form(SHOP_SESSION, "POST", "/resellers/5/subscription/sync")
        self.assertIn("ok=", ok.headers["location"])
        self.assertNotIn("ok=", denied.headers["location"])
        async with self.Session() as session:
            row = (await session.scalars(select(PgAdminSubscription))).one()
        self.assertEqual((row.base_gb, row.base_users), (500, 50))

    async def test_an_unreadable_panel_is_reported_and_nothing_changes(self):
        with patch("app.services.reseller_capacity_view.read_live_pg_admin_capacity", AsyncMock(side_effect=PgCapacityUnreadable("خوانده نشد"))):
            response = await self.call_form(OWNER_SESSION, "POST", "/resellers/5/subscription/sync")
        self.assertIn("err=", response.headers["location"])
        async with self.Session() as session:
            row = (await session.scalars(select(PgAdminSubscription))).one()
        self.assertEqual((row.base_gb, row.base_users), (0, 0))
