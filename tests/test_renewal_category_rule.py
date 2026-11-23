"""A renewal that carries leftover volume or time stays in the service's plan category."""

from __future__ import annotations

import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.db.models import BotUser, Order, Plan, PlanCategory, UserService
from app.services import service_renewals
from app.services.orders import renew_service_with_plan
from app.services.renewal_category import (
    CATEGORY_CHECK_FAILED,
    CATEGORY_SWITCH_REFUSED,
    Leftover,
    assert_category_switch,
    cached_leftover,
    offered_plans,
)
from app.services.service_renewals import calculate_renewal, renewal_terms
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id
from tests.postgres_support import scoped_async_engine

GB = 1024**3
NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def plan(plan_id: int, category: int | None, *, volume: bool = True, time_: bool = True, gb: int = 30, days: int = 30):
    return SimpleNamespace(id=plan_id, category_id=category, data_limit_gb=gb, duration_days=days,
                           carry_remaining_volume=volume, carry_remaining_time=time_)


CURRENT = plan(1, 10)
SAME = plan(2, 10)
OTHER_CARRIES = plan(3, 11)
OTHER_CARRIES_NOTHING = plan(4, 11, volume=False, time_=False)
OTHER_TIME_ONLY = plan(5, 11, volume=False)
UNCATEGORIZED = plan(6, None)
ALL = [CURRENT, SAME, OTHER_CARRIES, OTHER_CARRIES_NOTHING, OTHER_TIME_ONLY, UNCATEGORIZED]


def ids(plans) -> list[int]:
    return [p.id for p in plans]


class ListingTests(unittest.TestCase):
    def test_with_leftover_only_the_own_category_and_plans_that_carry_nothing(self) -> None:
        offered = offered_plans(ALL, current_plan=CURRENT, leftover=Leftover(volume=True, time=True))
        self.assertEqual(ids(offered), [1, 2, 4])

    def test_a_used_up_service_may_switch_to_any_category(self) -> None:
        self.assertEqual(ids(offered_plans(ALL, current_plan=CURRENT, leftover=Leftover(False, False))), ids(ALL))

    def test_only_the_part_still_left_decides(self) -> None:
        # Volume left, time over: a plan that carries only time carries nothing.
        offered = offered_plans(ALL, current_plan=CURRENT, leftover=Leftover(volume=True, time=False))
        self.assertEqual(ids(offered), [1, 2, 4, 5])

    def test_a_service_without_a_known_plan_keeps_every_choice(self) -> None:
        self.assertEqual(ids(offered_plans(ALL, current_plan=None, leftover=Leftover(True, True))), ids(ALL))

    def test_the_quota_cache_decides_what_is_left_and_unknown_counts_as_left(self) -> None:
        self.assertEqual(cached_leftover(SimpleNamespace(quota_synced_at=None), now=NOW), Leftover(True, True))
        synced = {"quota_synced_at": NOW, "quota_data_limit_bytes": 50 * GB}
        active = SimpleNamespace(**synced, quota_status="active", quota_expire_at=NOW + timedelta(days=3))
        self.assertEqual(cached_leftover(active, now=NOW), Leftover(True, True))
        used = SimpleNamespace(**synced, quota_status="limited", quota_expire_at=NOW + timedelta(days=3))
        self.assertEqual(cached_leftover(used, now=NOW), Leftover(False, True))
        expired = SimpleNamespace(**synced, quota_status="active", quota_expire_at=NOW - timedelta(days=1))
        self.assertEqual(cached_leftover(expired, now=NOW), Leftover(True, False))
        held = SimpleNamespace(**synced, quota_status="on_hold", quota_expire_at=None)
        self.assertEqual(cached_leftover(held, now=NOW), Leftover(True, True))


class LiveRuleTests(unittest.TestCase):
    def renewal(self, target, *, used_gb: float, days_left: float) -> dict:
        info = {"status": "active", "data_limit": 50 * GB, "used_traffic": int(used_gb * GB),
                "expire": int(time.time() + days_left * 86400)}
        return calculate_renewal(info, renewal_terms(target))

    def test_carrying_leftover_into_another_category_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, CATEGORY_SWITCH_REFUSED):
            assert_category_switch(CURRENT, OTHER_CARRIES, self.renewal(OTHER_CARRIES, used_gb=10, days_left=5))

    def test_another_category_is_fine_when_nothing_is_carried(self) -> None:
        assert_category_switch(CURRENT, OTHER_CARRIES, self.renewal(OTHER_CARRIES, used_gb=50, days_left=0))
        assert_category_switch(CURRENT, OTHER_CARRIES_NOTHING, self.renewal(OTHER_CARRIES_NOTHING, used_gb=10, days_left=5))

    def test_the_own_category_always_carries(self) -> None:
        assert_category_switch(CURRENT, SAME, self.renewal(SAME, used_gb=10, days_left=5))


class FakePanel:
    def __init__(self, info: dict | None = None, error: Exception | None = None) -> None:
        self.calls = 0
        self.info, self.error = info, error

    async def get_user_by_id(self, _user_id: int) -> dict:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return dict(self.info)


class OrderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([PlanCategory(id=10, name="ویژه"), PlanCategory(id=11, name="اقتصادی")])
        await self.session.flush()
        self.session.add_all([
            BotUser(id=1, telegram_id=101, referral_code="u1"),
            Plan(id=1, name="ویژه ۳۰", price=300, data_limit_gb=30, duration_days=30, category_id=10, is_active=True),
            Plan(id=2, name="ویژه ۵۰", price=500, data_limit_gb=50, duration_days=30, category_id=10, is_active=True),
            Plan(id=3, name="اقتصادی ۳۰", price=100, data_limit_gb=30, duration_days=30, category_id=11, is_active=True),
            UserService(id=70, bot_user_id=1, plan_id=1, pg_username="me70", pg_user_id=700),
        ])
        await self.session.commit()
        self.shop_token = set_shop_reseller_id(None)

    async def asyncTearDown(self) -> None:
        reset_shop_reseller_id(self.shop_token)
        await self.session.close()
        await self.engine.dispose()

    async def renew(self, plan_id: int, panel: FakePanel) -> Order:
        service = await self.session.get(UserService, 70)
        target = await self.session.get(Plan, plan_id)
        with patch("app.services.pasarguard.get_pg", return_value=panel), \
                patch.object(service_renewals, "assert_service_quota_available", AsyncMock()):
            return await renew_service_with_plan(self.session, user_id=1, service=service, plan=target)

    async def orders(self) -> int:
        return int(await self.session.scalar(select(func.count(Order.id))))

    def live(self, *, used_gb: float, days_left: float) -> FakePanel:
        return FakePanel({"status": "active", "data_limit": 30 * GB, "used_traffic": int(used_gb * GB),
                          "expire": int(time.time() + days_left * 86400)})

    async def test_another_category_with_leftover_is_refused_and_no_order_is_made(self) -> None:
        with self.assertRaisesRegex(ValueError, CATEGORY_SWITCH_REFUSED):
            await self.renew(3, self.live(used_gb=5, days_left=10))
        self.assertEqual(await self.orders(), 0)

    async def test_an_unreadable_panel_refuses_the_switch(self) -> None:
        with self.assertRaisesRegex(ValueError, CATEGORY_CHECK_FAILED):
            await self.renew(3, FakePanel(error=httpx.ConnectError("refused")))
        self.assertEqual(await self.orders(), 0)

    async def test_a_used_up_service_may_renew_in_another_category(self) -> None:
        order = await self.renew(3, self.live(used_gb=30, days_left=-1))
        self.assertEqual(order.plan_id, 3)

    async def test_the_own_category_never_reads_the_panel(self) -> None:
        panel = FakePanel(error=httpx.ConnectError("refused"))
        order = await self.renew(2, panel)
        self.assertEqual((order.plan_id, panel.calls), (2, 0))

    async def test_automatic_renewal_offers_and_keeps_only_plans_that_may_take_the_leftover(self) -> None:
        from app.db.models import ServiceAutomation
        from app.services.service_automation import automation_choices, selected_choice

        service = await self.session.get(UserService, 70)
        choices = await automation_choices(self.session, service)
        self.assertEqual(sorted(p.id for p in choices["renew"]), [1, 2])
        for plan_id, allowed in ((2, True), (3, False)):
            row = ServiceAutomation(service_id=70, shop_id=None, renew_plan_id=plan_id)
            chosen = await selected_choice(self.session, row, "renew")
            self.assertEqual(chosen is not None, allowed, plan_id)


if __name__ == "__main__":
    unittest.main()
