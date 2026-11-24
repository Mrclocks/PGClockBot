"""Discount budget, win-back coupons and the discount report."""

from __future__ import annotations

import asyncio
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Base, BotUser, DiscountRule, LoyaltyDiscountEntitlement, Order, OrderStatus, Plan
from app.services.coupon_programs import TRIGGER_NEXT_PURCHASE, TRIGGER_WIN_BACK, ProgramInput, issue_coupon, save_program, validate_program
from app.services.coupon_winback import issue_winback_coupons
from app.services.discount_budget import budget_used, save_monthly_budget
from app.services.discount_report import coupon_results, rule_results
from app.services.orders import cancel_order, create_order
from app.services.users import _SETTINGS_CACHE, reset_shop_reseller_id, set_shop_reseller_id
from tests.postgres_support import scoped_async_engine, scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
SHOP_A, SHOP_B = 50, 60
WIN_BACK = ProgramInput(True, 15, None, 0, 14, None, inactive_days=30)


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.token = set_shop_reseller_id(None)
        _SETTINGS_CACHE.clear()
        async with self.Session() as session:
            session.add_all([BotUser(id=s, telegram_id=s, referral_code=f"s{s}", role="reseller") for s in (SHOP_A, SHOP_B)])
            session.add_all([BotUser(id=i, telegram_id=100 + i, referral_code=f"u{i}") for i in (1, 2, 3)])
            await session.flush()
            session.add(Plan(id=1, name="پلن", price=100_000, duration_days=30, data_limit_gb=10))
            await session.commit()
        self.contact = patch("app.services.orders.require_purchase_contact", AsyncMock())
        self.contact.start()

    async def asyncTearDown(self) -> None:
        self.contact.stop()
        reset_shop_reseller_id(self.token)
        _SETTINGS_CACHE.clear()
        await self.engine.dispose()

    async def delivered(self, session, *, user_id: int, shop: int | None, days_ago: int) -> Order:
        stamp = datetime.now(timezone.utc) - timedelta(days=days_ago)
        order = Order(user_id=user_id, plan_id=1, amount=100_000, reseller_id=shop,
                      status=OrderStatus.DELIVERED.value, delivered_at=stamp)
        session.add(order)
        await session.commit()
        return order


class BudgetTests(_Base):
    async def add_rule(self, session) -> DiscountRule:
        rule = DiscountRule(name="ده درصد", audience="all", events="new", mode="percent", percent_bps=1000)
        session.add(rule)
        await session.commit()
        return rule

    async def test_rule_stops_when_the_month_budget_is_spent_and_cancel_gives_it_back(self) -> None:
        async with self.Session() as session:
            await self.add_rule(session)
            await save_monthly_budget(session, None, 15_000)
            first = await create_order(session, user_id=1, plan_id=1)
            second = await create_order(session, user_id=2, plan_id=1)
            self.assertEqual((first.amount, second.amount), (90_000, 100_000))
            await cancel_order(session, first)
            await session.commit()
            third = await create_order(session, user_id=3, plan_id=1)
            used = await budget_used(session, None, at=datetime.now(timezone.utc))
        self.assertEqual((third.amount, used), (90_000, 10_000))

    async def test_spend_is_counted_without_a_limit(self) -> None:
        async with self.Session() as session:
            await self.add_rule(session)
            await create_order(session, user_id=1, plan_id=1)
            self.assertEqual(await budget_used(session, None, at=datetime.now(timezone.utc)), 10_000)
            self.assertEqual(await budget_used(session, SHOP_A, at=datetime.now(timezone.utc)), 0)

    async def test_concurrent_checkouts_never_overspend(self) -> None:
        async with self.Session() as session:
            await self.add_rule(session)
            await save_monthly_budget(session, None, 10_000)

        async def buy(user_id: int) -> int:
            async with self.Session() as session:
                return (await create_order(session, user_id=user_id, plan_id=1)).amount

        self.assertEqual(sorted(await asyncio.gather(buy(1), buy(2))), [90_000, 100_000])

    async def test_budget_out_of_range_is_refused(self) -> None:
        async with self.Session() as session:
            for amount in (-1, 3_000_000_000, True):
                with self.subTest(amount=amount), self.assertRaises(ValueError):
                    await save_monthly_budget(session, None, amount)


class WinBackTests(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot = SimpleNamespace(send_message=AsyncMock(), session=SimpleNamespace(close=AsyncMock()))
        self.open_bot = patch("app.services.coupon_winback.open_notify_bot_for_user",
                              AsyncMock(return_value=(self.bot, False)))
        self.open_bot.start()

    async def asyncTearDown(self) -> None:
        self.open_bot.stop()
        await super().asyncTearDown()

    async def coupons(self, session) -> list[tuple[int, int | None]]:
        rows = await session.execute(select(LoyaltyDiscountEntitlement.user_id, LoyaltyDiscountEntitlement.reseller_id)
                                     .order_by(LoyaltyDiscountEntitlement.id))
        return [tuple(row) for row in rows.all()]

    async def test_lapsed_customer_gets_one_coupon_per_lapse(self) -> None:
        async with self.Session() as session:
            await save_program(session, SHOP_A, TRIGGER_WIN_BACK, WIN_BACK)
            await self.delivered(session, user_id=1, shop=SHOP_A, days_ago=40)
            await self.delivered(session, user_id=2, shop=SHOP_A, days_ago=5)
            self.assertEqual(await issue_winback_coupons(session), 1)
            self.assertEqual(await issue_winback_coupons(session), 0)
            self.assertEqual(await self.coupons(session), [(1, SHOP_A)])
            await self.delivered(session, user_id=1, shop=SHOP_A, days_ago=31)
            self.assertEqual(await issue_winback_coupons(session), 1)
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(self.bot.send_message.await_args.args[0], 101)

    async def test_purchases_and_programs_of_another_shop_are_ignored(self) -> None:
        async with self.Session() as session:
            await save_program(session, SHOP_A, TRIGGER_WIN_BACK, WIN_BACK)
            await self.delivered(session, user_id=1, shop=SHOP_B, days_ago=90)
            await self.delivered(session, user_id=2, shop=None, days_ago=90)
            self.assertEqual(await issue_winback_coupons(session), 0)

    async def test_disabled_program_issues_nothing(self) -> None:
        async with self.Session() as session:
            await save_program(session, SHOP_A, TRIGGER_WIN_BACK, ProgramInput(False, 15, None, 0, 14, None, 30))
            await self.delivered(session, user_id=1, shop=SHOP_A, days_ago=90)
            self.assertEqual(await issue_winback_coupons(session), 0)

    async def test_one_failed_customer_does_not_stop_the_rest(self) -> None:
        async with self.Session() as session:
            await save_program(session, SHOP_A, TRIGGER_WIN_BACK, WIN_BACK)
            for user_id in (1, 2, 3):
                await self.delivered(session, user_id=user_id, shop=SHOP_A, days_ago=60)
            real = issue_coupon

            async def flaky(session, **kw):
                if kw["user"].id == 2:
                    raise sa.exc.OperationalError("x", {}, None)
                return await real(session, **kw)

            with patch("app.services.coupon_winback.issue_coupon", flaky):
                self.assertEqual(await issue_winback_coupons(session), 2)
            self.assertEqual(sorted(u for u, _ in await self.coupons(session)), [1, 3])

    def test_win_back_needs_inactive_days(self) -> None:
        with self.assertRaises(ValueError):
            validate_program(TRIGGER_WIN_BACK, ProgramInput(True, 15, None, 0, 14, None, inactive_days=0))
        validate_program(TRIGGER_WIN_BACK, WIN_BACK)


class ReportTests(_Base):
    async def test_rule_results_count_only_delivered_orders_of_the_shop(self) -> None:
        async with self.Session() as session:
            rule = DiscountRule(name="ده", audience="all", events="new", mode="percent", percent_bps=1000)
            session.add(rule)
            await session.commit()
            paid = await create_order(session, user_id=1, plan_id=1)
            await create_order(session, user_id=2, plan_id=1)
            paid.status = OrderStatus.DELIVERED.value
            await session.commit()
            since = datetime.now(timezone.utc) - timedelta(days=30)
            results = await rule_results(session, None, since=since)
            other = await rule_results(session, SHOP_A, since=since)
        self.assertEqual((results[rule.id].orders, results[rule.id].discount, results[rule.id].sales), (1, 10_000, 90_000))
        self.assertEqual(other, {})

    async def test_coupon_results_per_program_and_shop(self) -> None:
        async with self.Session() as session:
            await save_program(session, SHOP_A, TRIGGER_NEXT_PURCHASE, ProgramInput(True, 10, None, 0, 14, None))
            user = await session.get(BotUser, 1)
            used = await issue_coupon(session, shop_id=SHOP_A, trigger=TRIGGER_NEXT_PURCHASE, user=user,
                                      issue_key=f"{TRIGGER_NEXT_PURCHASE}:{SHOP_A}:1")
            await issue_coupon(session, shop_id=SHOP_A, trigger=TRIGGER_NEXT_PURCHASE, user=user,
                               issue_key=f"{TRIGGER_NEXT_PURCHASE}:{SHOP_A}:2")
            used.status = "consumed"
            await session.commit()
            since = datetime.now(timezone.utc) - timedelta(days=30)
            mine = await coupon_results(session, SHOP_A, since=since)
            other = await coupon_results(session, SHOP_B, since=since)
        self.assertEqual((mine[TRIGGER_NEXT_PURCHASE].issued, mine[TRIGGER_NEXT_PURCHASE].used), (2, 1))
        self.assertEqual(other[TRIGGER_NEXT_PURCHASE].issued, 0)


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keeps_programs(self) -> None:
        spec = importlib.util.spec_from_file_location("winback", ROOT / "alembic/versions/0130_discount_budget_winback.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE coupon_programs (id INTEGER PRIMARY KEY, trigger VARCHAR(24))"))
            conn.execute(text("INSERT INTO coupon_programs VALUES (1, 'next_purchase')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertEqual(conn.execute(text("SELECT inactive_days FROM coupon_programs")).scalar(), 0)
                conn.execute(text("INSERT INTO discount_budgets (shop_key, month) VALUES (0, '2026-11')"))
                with self.assertRaises(sa.exc.IntegrityError), conn.begin_nested():
                    conn.execute(text("INSERT INTO discount_budgets (shop_key, month) VALUES (0, '2026-11')"))
                migration.downgrade()
                self.assertFalse(sa.inspect(conn).has_table("discount_budgets"))
                self.assertEqual(conn.execute(text("SELECT count(*) FROM coupon_programs")).scalar(), 1)
                migration.upgrade()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
