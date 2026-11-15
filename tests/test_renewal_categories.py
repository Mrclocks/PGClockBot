"""Renewal offers the service's own plan first, then the shop's categories, like the shop."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import select

from app.bot.panel_history import panel_history
from app.db.models import BotUser, Plan, PlanCategory, UserService
from app.services.service_renewals import renewal_choices
from tests.chat_sim import ChatSim

BACK = "بازگشت"


def _plan(plan_id: int, category_id: int | None, *, gb: float = 50, days: int = 30) -> SimpleNamespace:
    return SimpleNamespace(id=plan_id, category_id=category_id, data_limit_gb=gb, duration_days=days)


class ChoicesTests(unittest.TestCase):
    def test_the_current_plan_comes_first_and_categories_keep_only_listed_ones(self) -> None:
        plans = [_plan(1, 10), _plan(2, 10, gb=100), _plan(3, None)]
        categories = [SimpleNamespace(id=10), SimpleNamespace(id=11)]
        choices = renewal_choices(plans, categories, current_plan_id=2)
        self.assertEqual(choices.current.id, 2)
        self.assertEqual([p.id for p in choices.options], [2, 1, 3])
        self.assertEqual([c.id for c in choices.categories], [10])
        self.assertEqual([p.id for p in choices.in_category(10)], [2, 1])
        self.assertEqual([p.id for p in choices.uncategorized], [3])
        self.assertIsNone(choices.category(11))

    def test_a_retired_plan_has_no_current_button(self) -> None:
        choices = renewal_choices([_plan(1, 10)], [SimpleNamespace(id=10)], current_plan_id=99)
        self.assertIsNone(choices.current)


class _RenewalFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        panel_history.clear()
        self.addCleanup(panel_history.clear)
        patcher = patch("app.bot.middlewares._rate_limited", MagicMock(return_value=False))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sim = ChatSim(5901)
        await self.sim.start(owner=False)
        self.addAsyncCleanup(self.sim.stop)
        await self.sim.type("/start")
        async with self.sim.Session() as session:
            me = await session.scalar(select(BotUser).where(BotUser.telegram_id == 5901))
            session.add_all([
                BotUser(id=950, telegram_id=9500, referral_code="shop950", role="reseller"),
                BotUser(id=951, telegram_id=9510, referral_code="other951"),
                PlanCategory(id=10, name="اقتصادی", owner_reseller_id=None, audience="users"),
                PlanCategory(id=11, name="ویژه", owner_reseller_id=None, audience="users"),
                PlanCategory(id=12, name="خالی", owner_reseller_id=None, audience="users"),
                PlanCategory(id=13, name="فروشگاه دیگر", owner_reseller_id=950, audience="users"),
                Plan(id=1, name="ماهانه ۵۰", price=100, data_limit_gb=50, duration_days=30, category_id=10, is_active=True),
                Plan(id=2, name="ماهانه ۲۰", price=80, data_limit_gb=20, duration_days=30, category_id=10, is_active=True),
                Plan(id=3, name="سه‌ماهه", price=300, data_limit_gb=200, duration_days=90, category_id=11, is_active=True),
                Plan(id=4, name="بدون دسته", price=90, data_limit_gb=40, duration_days=30, is_active=True),
                Plan(id=5, name="پلن فروشگاه", price=90, data_limit_gb=40, duration_days=30, category_id=13,
                     owner_reseller_id=950, is_active=True),
            ])
            await session.flush()
            session.add_all([
                UserService(id=70, bot_user_id=me.id, plan_id=1, pg_username="me70"),
                UserService(id=71, bot_user_id=951, plan_id=1, pg_username="other71"),
            ])
            await session.commit()
        self.panel = [m for m in self.sim.telegram.visible() if m.from_bot][-1]

    async def tap(self, data: str) -> None:
        await self.sim._tap(self.sim.telegram.messages[self.panel.message_id], data)

    def buttons(self) -> list[tuple[str, str]]:
        return [b for row in self.sim.telegram.messages[self.panel.message_id].inline for b in row]


class GroupedRenewalTests(_RenewalFlow):
    async def test_the_current_plan_then_the_categories(self) -> None:
        await self.tap("svc:renew:70")

        texts = [t for t, _ in self.buttons()]
        self.assertTrue(texts[0].startswith("🔄 پلن فعلی · ماهانه ۵۰"), texts)
        self.assertEqual(self.buttons()[0][1], "svc:renewpay:70:1")
        self.assertEqual([d for _, d in self.buttons()[1:4]], ["svc:renewcat:70:10", "svc:renewcat:70:11", "svc:renewcat:70:none"])
        self.assertEqual([t for t in texts[1:4]], ["اقتصادی", "ویژه", "سایر"])
        self.assertNotIn("خالی", texts)
        self.assertNotIn("فروشگاه دیگر", texts)
        self.assertNotIn("سه‌ماهه", " ".join(texts))

    async def test_a_category_lists_its_plans_with_their_relation_and_back_returns(self) -> None:
        await self.tap("svc:renew:70")
        await self.tap("svc:renewcat:70:10")

        texts = [t for t, _ in self.buttons()]
        self.assertTrue(texts[0].startswith("🔄 پلن فعلی · ماهانه ۵۰"), texts)
        self.assertTrue(texts[1].startswith("⬇️ تنزل · ماهانه ۲۰"), texts)
        backs = [d for t, d in self.buttons() if BACK in t]
        self.assertEqual(backs, ["svc:renew:70"])
        await self.tap("svc:renew:70")
        self.assertEqual(self.buttons()[1][1], "svc:renewcat:70:10")

    async def test_plans_without_a_category_are_under_other(self) -> None:
        await self.tap("svc:renew:70")
        await self.tap("svc:renewcat:70:none")
        self.assertEqual([d for _, d in self.buttons() if d.startswith("svc:renewpay:")], ["svc:renewpay:70:4"])

    async def test_another_shops_or_an_empty_category_does_not_open(self) -> None:
        await self.tap("svc:renew:70")
        before = self.buttons()
        for data in ("svc:renewcat:70:13", "svc:renewcat:70:12", "svc:renewcat:70:999"):
            with self.subTest(data=data):
                await self.tap(data)
                self.assertEqual(self.buttons(), before)
        self.assertNotIn("پلن فروشگاه", " ".join(t for t, _ in self.buttons()))
        self.assertTrue(self.sim.telegram.alerts)

    async def test_another_customers_service_does_not_open(self) -> None:
        await self.tap("svc:renewcat:70:10")
        before = self.buttons()
        await self.tap("svc:renewcat:71:10")
        self.assertEqual(self.buttons(), before)
        self.assertTrue(self.sim.telegram.alerts)


class UngroupedRenewalTests(_RenewalFlow):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        async with self.sim.Session() as session:
            for plan in (await session.scalars(select(Plan))).all():
                plan.category_id = None
            await session.commit()

    async def test_without_categories_every_plan_is_listed_as_before(self) -> None:
        await self.tap("svc:renew:70")
        data = [d for _, d in self.buttons() if d.startswith("svc:renew")]
        self.assertEqual(data[0], "svc:renewpay:70:1")
        self.assertEqual(sorted(data), ["svc:renewpay:70:1", "svc:renewpay:70:2", "svc:renewpay:70:3", "svc:renewpay:70:4"])


if __name__ == "__main__":
    unittest.main()
