"""A service is renewed only within its own plan category, whatever would or would not carry over."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.db.models import BotUser, Order, Plan, PlanCategory, ServiceAutomation, UserService
from app.services import service_renewals
from app.services.orders import renew_service_with_plan
from app.services.renewal_category import OWN_CATEGORY_ONLY, offered_plans, refuse_other_category
from app.services.service_automation import automation_choices, selected_choice
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id
from tests.postgres_support import scoped_async_engine


def plan(plan_id: int, category: int | None, *, volume: bool = True, time_: bool = True, gb: int | None = 30):
    return SimpleNamespace(id=plan_id, category_id=category, data_limit_gb=gb, duration_days=30,
                           carry_remaining_volume=volume, carry_remaining_time=time_)


CURRENT = plan(1, 10)
SAME = plan(2, 10)
OTHER_CARRIES = plan(3, 11)
# Unlimited and carrying nothing: the case that was still offered before.
OTHER_CARRIES_NOTHING = plan(4, 11, volume=False, time_=False, gb=None)
UNCATEGORIZED = plan(6, None)
ALL = [CURRENT, SAME, OTHER_CARRIES, OTHER_CARRIES_NOTHING, UNCATEGORIZED]


def ids(plans) -> list[int]:
    return [p.id for p in plans]


class RuleTests(unittest.TestCase):
    def test_only_the_own_category_is_offered(self) -> None:
        self.assertEqual(ids(offered_plans(ALL, current_plan=CURRENT)), [1, 2])

    def test_another_category_is_not_offered_even_when_it_carries_nothing(self) -> None:
        self.assertNotIn(4, ids(offered_plans(ALL, current_plan=CURRENT)))

    def test_an_empty_own_category_offers_nothing(self) -> None:
        self.assertEqual(offered_plans([OTHER_CARRIES, OTHER_CARRIES_NOTHING, UNCATEGORIZED], current_plan=CURRENT), [])

    def test_an_uncategorized_service_is_offered_only_uncategorized_plans(self) -> None:
        self.assertEqual(ids(offered_plans(ALL, current_plan=plan(7, None))), [6])

    def test_a_service_without_a_known_plan_keeps_every_choice(self) -> None:
        self.assertEqual(ids(offered_plans(ALL, current_plan=None)), ids(ALL))

    def test_an_order_for_another_category_is_refused(self) -> None:
        for target in (OTHER_CARRIES, OTHER_CARRIES_NOTHING, UNCATEGORIZED):
            with self.assertRaisesRegex(ValueError, OWN_CATEGORY_ONLY):
                refuse_other_category(CURRENT, target)
        refuse_other_category(CURRENT, SAME)
        refuse_other_category(None, OTHER_CARRIES)


class FakePanel:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    async def get_user_by_id(self, _user_id: int) -> dict:
        self.calls += 1
        raise self.error or httpx.ConnectError("not expected")


class OrderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([PlanCategory(id=10, name="حجمی"), PlanCategory(id=11, name="نامحدود")])
        await self.session.flush()
        self.session.add_all([
            BotUser(id=1, telegram_id=101, referral_code="u1"),
            Plan(id=1, name="حجمی ۳۰", price=300, data_limit_gb=30, duration_days=30, category_id=10, is_active=True),
            Plan(id=2, name="حجمی ۵۰", price=500, data_limit_gb=50, duration_days=30, category_id=10, is_active=True),
            Plan(id=3, name="نامحدود", price=100, data_limit_gb=None, duration_days=30, category_id=11, is_active=True,
                 carry_remaining_volume=False, carry_remaining_time=False),
            UserService(id=70, bot_user_id=1, plan_id=1, pg_username="me70", pg_user_id=700),
        ])
        await self.session.commit()
        self.shop_token = set_shop_reseller_id(None)

    async def asyncTearDown(self) -> None:
        reset_shop_reseller_id(self.shop_token)
        await self.session.close()
        await self.engine.dispose()

    async def renew(self, plan_id: int, panel: FakePanel | None = None) -> Order:
        service = await self.session.get(UserService, 70)
        target = await self.session.get(Plan, plan_id)
        with patch("app.services.pasarguard.get_pg", return_value=panel or FakePanel()), \
                patch.object(service_renewals, "assert_service_quota_available", AsyncMock()):
            return await renew_service_with_plan(self.session, user_id=1, service=service, plan=target)

    async def orders(self) -> int:
        return int(await self.session.scalar(select(func.count(Order.id))))

    async def test_another_category_carrying_nothing_is_refused_without_reading_the_panel(self) -> None:
        panel = FakePanel()
        with self.assertRaisesRegex(ValueError, OWN_CATEGORY_ONLY):
            await self.renew(3, panel)
        self.assertEqual((panel.calls, await self.orders()), (0, 0))

    async def test_an_empty_own_category_still_refuses_another_category(self) -> None:
        for plan_id in (1, 2):
            (await self.session.get(Plan, plan_id)).is_active = False
        await self.session.commit()
        with self.assertRaisesRegex(ValueError, OWN_CATEGORY_ONLY):
            await self.renew(3)
        self.assertEqual(await self.orders(), 0)

    async def test_the_own_category_renews_without_reading_the_panel(self) -> None:
        panel = FakePanel()
        order = await self.renew(2, panel)
        self.assertEqual((order.plan_id, panel.calls), (2, 0))

    async def test_automatic_renewal_offers_and_keeps_only_the_own_category(self) -> None:
        service = await self.session.get(UserService, 70)
        choices = await automation_choices(self.session, service)
        self.assertEqual(sorted(p.id for p in choices["renew"]), [1, 2])
        for plan_id, allowed in ((2, True), (3, False)):
            row = ServiceAutomation(service_id=70, shop_id=None, renew_plan_id=plan_id)
            self.assertEqual(await selected_choice(self.session, row, "renew") is not None, allowed, plan_id)


if __name__ == "__main__":
    unittest.main()
