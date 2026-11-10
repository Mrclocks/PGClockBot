"""Cashback: one grant per delivered order, inside its own shop, within budget, reversed once."""

from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, CashbackGrant, CashbackRule, Order, OrderStatus, ShopWallet
from app.services.cashback import (
    Purchase,
    combine,
    grant_for_order,
    mature_grants,
    order_event,
    reverse_order_cashback,
    rule_amount,
)
from app.services.users import set_setting

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
SHOP_A, SHOP_B, BUYER = 5, 6, 9


def rule(**kw) -> SimpleNamespace:
    base = dict(id=1, enabled=True, mode="percent", percent_bps=1000, fixed_amount=0, max_per_order=None,
                min_order_amount=0, events="new,renew,addon", plan_ids=None, payment_methods=None,
                first_purchase_only=False, starts_at=None, ends_at=None, allow_with_discount=True, priority=0)
    base.update(kw)
    return SimpleNamespace(**base)


def purchase(**kw) -> Purchase:
    base = dict(amount=100_000, event="new", plan_id=3, payment_method="card", has_discount=False,
                first_purchase=False, at=NOW)
    base.update(kw)
    return Purchase(**base)


class RuleTests(unittest.TestCase):
    def test_percent_fixed_and_cap(self) -> None:
        self.assertEqual(rule_amount(rule(), purchase()), 10_000)
        self.assertEqual(rule_amount(rule(max_per_order=5_000), purchase()), 5_000)
        self.assertEqual(rule_amount(rule(mode="fixed", fixed_amount=300_000), purchase()), 100_000)
        self.assertEqual(rule_amount(rule(percent_bps=99_999), purchase()), 100_000)

    def test_conditions(self) -> None:
        refused = [
            rule(enabled=False), rule(min_order_amount=200_000), rule(events="renew"), rule(plan_ids="4,5"),
            rule(payment_methods="crypto"), rule(first_purchase_only=True), rule(starts_at=NOW + timedelta(days=1)),
            rule(ends_at=NOW),
        ]
        for case in refused:
            with self.subTest(case=case):
                self.assertEqual(rule_amount(case, purchase()), 0)
        self.assertEqual(rule_amount(rule(allow_with_discount=False), purchase(has_discount=True)), 0)
        self.assertEqual(rule_amount(rule(), purchase(amount=0)), 0)
        self.assertEqual(rule_amount(rule(plan_ids="3"), purchase()), 10_000)

    def test_best_or_sum(self) -> None:
        a, b = rule(id=1), rule(id=2)
        self.assertEqual(combine([(a, 5_000), (b, 8_000)], mode="best", price=100_000), (2, 8_000))
        self.assertEqual(combine([(a, 70_000), (b, 80_000)], mode="sum", price=100_000), (None, 100_000))
        self.assertEqual(combine([(a, 0)], mode="best", price=1), (None, 0))

    def test_event_from_order_note(self) -> None:
        self.assertEqual(order_event(SimpleNamespace(note="renew:4")), "renew")
        self.assertEqual(order_event(SimpleNamespace(note="svc_addon:1:2:volume:10")), "addon")
        self.assertEqual(order_event(SimpleNamespace(note=None)), "new")


class CashbackCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'k.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP_A, telegram_id=5, referral_code="a", role="reseller"),
                BotUser(id=SHOP_B, telegram_id=6, referral_code="b", role="reseller"),
                BotUser(id=BUYER, telegram_id=9, referral_code="c"),
                CashbackRule(id=1, shop_id=SHOP_A, name="10%", percent_bps=1000),
                CashbackRule(id=2, shop_id=SHOP_B, name="50%", percent_bps=5000),
            ])
            for n in range(1, 6):
                session.add(Order(id=n, user_id=BUYER, plan_id=3, amount=100_000, reseller_id=SHOP_A,
                                  status=OrderStatus.DELIVERED.value, payment_method="card"))
            await session.commit()
            await set_setting(session, "cashback_enabled", "1", reseller_id=SHOP_A)
            await session.commit()
        notices = patch("app.services.cashback.notify_credited", AsyncMock())
        notices.start()
        self.addCleanup(notices.stop)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def grant(self, order_id: int, at: datetime = NOW):
        async with self.Session() as session:
            return await grant_for_order(session, await session.get(Order, order_id), now=at)

    async def shop_wallet(self, shop: int = SHOP_A) -> int:
        async with self.Session() as session:
            return int(await session.scalar(select(func.coalesce(func.sum(ShopWallet.balance), 0)).where(
                ShopWallet.user_id == BUYER, ShopWallet.reseller_id == shop)) or 0)

    async def grants(self) -> list[CashbackGrant]:
        async with self.Session() as session:
            return list((await session.scalars(select(CashbackGrant).order_by(CashbackGrant.id))).all())


class GrantTests(CashbackCase):
    async def test_one_pending_grant_per_order_from_the_shops_own_rule(self) -> None:
        first = await self.grant(1)
        again = await self.grant(1)
        self.assertEqual((first.id, first.amount, first.rule_id, first.status), (again.id, 10_000, 1, "pending"))
        self.assertEqual(first.available_at.replace(tzinfo=timezone.utc), NOW + timedelta(hours=24))
        self.assertEqual(await self.shop_wallet(), 0)

    async def test_concurrent_grants_for_one_order_make_one(self) -> None:
        await asyncio.gather(*(self.grant(2) for _ in range(3)), return_exceptions=True)
        self.assertEqual(len(await self.grants()), 1)

    async def test_disabled_shop_and_undelivered_orders_earn_nothing(self) -> None:
        async with self.Session() as session:
            await set_setting(session, "cashback_enabled", "0", reseller_id=SHOP_A)
            await session.commit()
        self.assertIsNone(await self.grant(1))
        async with self.Session() as session:
            await set_setting(session, "cashback_enabled", "1", reseller_id=SHOP_A)
            (await session.get(Order, 3)).status = OrderStatus.PAID.value
            await session.commit()
        self.assertIsNone(await self.grant(3))

    async def test_monthly_budget_is_never_exceeded(self) -> None:
        async with self.Session() as session:
            await set_setting(session, "cashback_monthly_budget", "15000", reseller_id=SHOP_A)
            await session.commit()
        self.assertIsNotNone(await self.grant(1))
        self.assertIsNone(await self.grant(2))
        self.assertIsNotNone(await self.grant(3, at=NOW + timedelta(days=30)))  # a new month

    async def test_customer_monthly_cap(self) -> None:
        async with self.Session() as session:
            (await session.get(CashbackRule, 1)).customer_max_count = 1
            await session.commit()
        self.assertIsNotNone(await self.grant(1))
        self.assertIsNone(await self.grant(2))


class MatureAndReverseTests(CashbackCase):
    async def test_credit_once_after_the_hold_into_the_shop_purse_only(self) -> None:
        await self.grant(1)
        async with self.Session() as session:
            self.assertEqual(await mature_grants(session, now=NOW + timedelta(hours=1)), 0)
            self.assertEqual(await mature_grants(session, now=NOW + timedelta(hours=25)), 1)
            self.assertEqual(await mature_grants(session, now=NOW + timedelta(hours=26)), 0)
        self.assertEqual(await self.shop_wallet(), 10_000)
        self.assertEqual(await self.shop_wallet(SHOP_B), 0)

    async def test_pending_cashback_is_cancelled_without_money(self) -> None:
        await self.grant(1)
        async with self.Session() as session:
            await reverse_order_cashback(session, 1, reason="void", now=NOW)
            await mature_grants(session, now=NOW + timedelta(hours=30))
        self.assertEqual([g.status for g in await self.grants()], ["cancelled"])
        self.assertEqual(await self.shop_wallet(), 0)

    async def test_credited_cashback_is_taken_back_once_and_shortfall_recorded(self) -> None:
        await self.grant(1)
        async with self.Session() as session:
            await mature_grants(session, now=NOW + timedelta(hours=25))
            wallet = await session.scalar(select(ShopWallet).where(ShopWallet.user_id == BUYER))
            wallet.balance = 4_000  # the buyer spent part of it
            await session.commit()
            await reverse_order_cashback(session, 1, reason="cancel", now=NOW)
            await reverse_order_cashback(session, 1, reason="cancel", now=NOW)
        [grant] = await self.grants()
        self.assertEqual((grant.status, grant.reversed_amount, grant.unrecoverable_amount), ("reversed", 4_000, 6_000))
        self.assertEqual(await self.shop_wallet(), 0)


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade(self) -> None:
        path = ROOT / "alembic/versions/0101_cashback.py"
        spec = importlib.util.spec_from_file_location("cashback_migration", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        engine = sa.create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
            for step in (module.upgrade, module.upgrade):
                with Operations.context(MigrationContext.configure(conn)):
                    step()
            for table in ("cashback_rules", "cashback_grants", "cashback_budgets"):
                self.assertTrue(sa.inspect(conn).has_table(table))
            with Operations.context(MigrationContext.configure(conn)):
                module.downgrade()
            self.assertFalse(sa.inspect(conn).has_table("cashback_grants"))


if __name__ == "__main__":
    unittest.main()
