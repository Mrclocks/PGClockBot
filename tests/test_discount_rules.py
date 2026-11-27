"""Automatic discount rules: who gets them, how much, once per customer, inside one shop."""

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

from app.db.models import Base, BotUser, DiscountCode, DiscountRule, Order, OrderStatus, Plan, UserService
from app.services.checkout_summary import checkout_summary
from app.services.discount_rules import (
    CODE_NOT_BETTER,
    CustomerHistory,
    RuleCheckout,
    apply_discount_rules,
    ranked_rules,
    rule_discount,
)
from app.services.order_discounts import live_order_discounts
from app.services.orders import apply_discount_to_order, cancel_order, create_order, renew_service_with_plan
from app.services.users import _SETTINGS_CACHE, reset_shop_reseller_id, set_shop_reseller_id
from tests.postgres_support import scoped_async_engine, scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
SHOP_A, SHOP_B = 50, 60


def rule(**kw) -> SimpleNamespace:
    base = dict(id=1, enabled=True, audience="all", min_purchases=0, audience_days=0, events="new,renew,addon",
                plan_ids=None, mode="percent", percent_bps=1000, fixed_amount=0, max_per_order=None,
                min_order_amount=0, customer_max_count=None, max_uses=None, used_count=0, ends_at=None, priority=0)
    base.update(kw)
    return SimpleNamespace(**base)


def checkout(**kw) -> RuleCheckout:
    base = dict(amount=100_000, event="new", plan_id=3, history=CustomerHistory(0, None),
                service_expires_at=None, at=NOW)
    base.update(kw)
    return RuleCheckout(**base)


class RuleMathTests(unittest.TestCase):
    def test_percent_fixed_cap_and_ninety_percent_ceiling(self) -> None:
        self.assertEqual(rule_discount(rule(), checkout()), 10_000)
        self.assertEqual(rule_discount(rule(max_per_order=4_000), checkout()), 4_000)
        self.assertEqual(rule_discount(rule(mode="fixed", fixed_amount=500_000), checkout()), 90_000)
        self.assertEqual(rule_discount(rule(percent_bps=10_000), checkout()), 90_000)

    def test_audiences(self) -> None:
        buyer = CustomerHistory(delivered_count=2, last_delivered_at=NOW - timedelta(days=40))
        cases = [
            (rule(audience="first"), checkout(), True),
            (rule(audience="first"), checkout(history=buyer), False),
            (rule(audience="repeat", min_purchases=2), checkout(history=buyer), True),
            (rule(audience="repeat", min_purchases=3), checkout(history=buyer), False),
            (rule(audience="returning", audience_days=30), checkout(history=buyer), True),
            (rule(audience="returning", audience_days=60), checkout(history=buyer), False),
            (rule(audience="returning", audience_days=1), checkout(), False),
            (rule(audience="early_renewal", audience_days=7),
             checkout(event="renew", service_expires_at=NOW + timedelta(days=3)), True),
            (rule(audience="early_renewal", audience_days=7),
             checkout(event="renew", service_expires_at=NOW + timedelta(days=10)), False),
            (rule(audience="early_renewal", audience_days=7),
             checkout(event="renew", service_expires_at=NOW - timedelta(days=1)), False),
            (rule(audience="early_renewal", audience_days=7), checkout(service_expires_at=NOW + timedelta(days=3)), False),
            (rule(audience="unknown"), checkout(), False),
        ]
        for case, purchase, expected in cases:
            with self.subTest(audience=case.audience, purchase=purchase):
                self.assertEqual(rule_discount(case, purchase) > 0, expected)

    def test_conditions(self) -> None:
        refused = [
            rule(enabled=False), rule(events="renew"), rule(plan_ids="4,5"), rule(min_order_amount=200_000),
            rule(ends_at=NOW), rule(max_uses=3, used_count=3),
        ]
        for case in refused:
            with self.subTest(case=case):
                self.assertEqual(rule_discount(case, checkout()), 0)
        self.assertEqual(rule_discount(rule(plan_ids="3"), checkout()), 10_000)
        self.assertEqual(rule_discount(rule(), checkout(amount=0)), 0)

    def test_best_rule_first_then_priority(self) -> None:
        small, big, tie = rule(id=1, percent_bps=500), rule(id=2, percent_bps=2000), rule(id=3, percent_bps=2000, priority=5)
        self.assertEqual([r.id for r, _ in ranked_rules([small, big, tie], checkout())], [3, 2, 1])


class DiscountRuleOrderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.token = set_shop_reseller_id(None)
        _SETTINGS_CACHE.clear()
        async with self.Session() as session:
            session.add_all([BotUser(id=i, telegram_id=100 + i, referral_code=f"u{i}") for i in (1, 2)])
            session.add_all([BotUser(id=s, telegram_id=s, referral_code=f"s{s}", role="reseller") for s in (SHOP_A, SHOP_B)])
            await session.flush()
            session.add_all([
                Plan(id=1, name="پلن", price=100_000, duration_days=30, data_limit_gb=10),
                Plan(id=2, name="پلن فروشگاه", price=100_000, duration_days=30, owner_reseller_id=SHOP_A),
                Plan(id=4, name="تست", price=0, is_trial=True),
                DiscountCode(code="BIG30", percent=30, is_active=True),
                DiscountCode(code="SMALL5", percent=5, is_active=True),
            ])
            await session.commit()
        self.contact = patch("app.services.orders.require_purchase_contact", AsyncMock())
        self.contact.start()

    async def asyncTearDown(self) -> None:
        self.contact.stop()
        reset_shop_reseller_id(self.token)
        _SETTINGS_CACHE.clear()
        await self.engine.dispose()

    async def add_rule(self, session, **kw) -> DiscountRule:
        values = dict(name="ده درصد", audience="all", events="new,renew,addon", mode="percent", percent_bps=1000)
        values.update(kw)
        row = DiscountRule(**values)
        session.add(row)
        await session.commit()
        return row

    async def deliver(self, session, *, user_id: int = 1, shop: int | None = None, days_ago: int = 1) -> None:
        stamp = datetime.now(timezone.utc) - timedelta(days=days_ago)
        session.add(Order(user_id=user_id, plan_id=1, amount=100_000, reseller_id=shop,
                          status=OrderStatus.DELIVERED.value, delivered_at=stamp))
        await session.commit()

    async def test_first_purchase_rule_lowers_a_new_order_and_records_its_line(self) -> None:
        async with self.Session() as session:
            first = await self.add_rule(session, name="خرید اول", audience="first", percent_bps=2000)
            order = await create_order(session, user_id=1, plan_id=1)
            lines = await live_order_discounts(session, order.id)
        self.assertEqual((order.amount, order.discount_amount), (80_000, 20_000))
        self.assertEqual([(line.source, line.source_ref, line.claim_key) for line in lines],
                         [("rule", str(first.id), "first")])

    async def test_first_purchase_discount_is_given_once_even_with_two_open_orders(self) -> None:
        async with self.Session() as session:
            await self.add_rule(session, audience="first", percent_bps=2000)
            first = await create_order(session, user_id=1, plan_id=1)
            second = await create_order(session, user_id=1, plan_id=1)
        self.assertEqual((first.amount, second.amount), (80_000, 100_000))

    async def test_concurrent_first_orders_get_one_first_purchase_discount(self) -> None:
        async with self.Session() as session:
            await self.add_rule(session, audience="first", percent_bps=2000)

        async def buy() -> int:
            async with self.Session() as session:
                return (await create_order(session, user_id=1, plan_id=1)).amount

        amounts = sorted(await asyncio.gather(buy(), buy()))
        self.assertEqual(amounts, [80_000, 100_000])

    async def test_cancelling_the_first_order_frees_the_first_purchase_discount(self) -> None:
        async with self.Session() as session:
            rule_row = await self.add_rule(session, audience="first", percent_bps=2000, max_uses=1)
            order = await create_order(session, user_id=1, plan_id=1)
            await cancel_order(session, order)
            await session.commit()
            again = await create_order(session, user_id=1, plan_id=1)
            await session.refresh(rule_row)
        self.assertEqual(again.amount, 80_000)
        self.assertEqual(rule_row.used_count, 1)

    async def test_claim_of_an_order_cancelled_without_release_is_healed(self) -> None:
        async with self.Session() as session:
            await self.add_rule(session, audience="first", percent_bps=2000)
            order = await create_order(session, user_id=1, plan_id=1)
            await session.execute(sa.update(Order).where(Order.id == order.id).values(status=OrderStatus.CANCELLED.value))
            await session.commit()
            again = await create_order(session, user_id=1, plan_id=1)
        self.assertEqual(again.amount, 80_000)

    async def test_customer_who_bought_before_gets_no_first_purchase_discount(self) -> None:
        async with self.Session() as session:
            await self.add_rule(session, audience="first", percent_bps=2000)
            await self.deliver(session)
            order = await create_order(session, user_id=1, plan_id=1)
        self.assertEqual(order.amount, 100_000)

    async def test_repeat_buyer_rule_and_per_customer_limit(self) -> None:
        async with self.Session() as session:
            await self.add_rule(session, audience="repeat", min_purchases=1, percent_bps=1500, customer_max_count=1)
            self.assertEqual((await create_order(session, user_id=1, plan_id=1)).amount, 100_000)
            await self.deliver(session)
            self.assertEqual((await create_order(session, user_id=1, plan_id=1)).amount, 85_000)
            self.assertEqual((await create_order(session, user_id=1, plan_id=1)).amount, 100_000)

    async def test_total_use_limit_stops_the_rule(self) -> None:
        async with self.Session() as session:
            await self.add_rule(session, max_uses=1)
            self.assertEqual((await create_order(session, user_id=1, plan_id=1)).amount, 90_000)
            self.assertEqual((await create_order(session, user_id=2, plan_id=1)).amount, 100_000)

    async def test_rule_of_another_shop_never_applies(self) -> None:
        async with self.Session() as session:
            await self.add_rule(session, shop_id=SHOP_B, percent_bps=5000)
            token = set_shop_reseller_id(SHOP_A)
            try:
                order = await create_order(session, user_id=1, plan_id=2)
            finally:
                reset_shop_reseller_id(token)
        self.assertEqual(order.amount, 100_000)

    async def test_shop_rule_applies_to_its_own_order_and_not_the_platform(self) -> None:
        async with self.Session() as session:
            await self.add_rule(session, shop_id=SHOP_A, percent_bps=5000)
            platform = await create_order(session, user_id=1, plan_id=1)
            token = set_shop_reseller_id(SHOP_A)
            try:
                shop = await create_order(session, user_id=1, plan_id=2)
            finally:
                reset_shop_reseller_id(token)
        self.assertEqual((platform.amount, shop.amount), (100_000, 50_000))

    async def test_purchases_in_another_shop_do_not_count_as_history(self) -> None:
        async with self.Session() as session:
            await self.add_rule(session, audience="first", percent_bps=2000)
            await self.deliver(session, shop=SHOP_A)
            order = await create_order(session, user_id=1, plan_id=1)
        self.assertEqual(order.amount, 80_000)

    async def test_trial_plan_is_never_discounted(self) -> None:
        async with self.Session() as session:
            await self.add_rule(session, mode="fixed", fixed_amount=1)
            order = Order(user_id=1, plan_id=4, amount=1000, status=OrderStatus.PENDING.value)
            session.add(order)
            await session.flush()
            self.assertIsNone(await apply_discount_rules(session, order))

    async def test_stronger_code_replaces_the_rule_and_weaker_code_is_refused(self) -> None:
        async with self.Session() as session:
            rule_row = await self.add_rule(session, percent_bps=1000, max_uses=5)
            order = await create_order(session, user_id=1, plan_id=1)
            with self.assertRaisesRegex(ValueError, CODE_NOT_BETTER):
                await apply_discount_to_order(session, order, "SMALL5")
            await session.refresh(order)
            self.assertEqual(order.amount, 90_000)
            order = await apply_discount_to_order(session, order, "BIG30")
            lines = await live_order_discounts(session, order.id)
            await session.refresh(rule_row)
            used = await session.scalar(select(DiscountCode.used_count).where(DiscountCode.code == "SMALL5"))
        self.assertEqual((order.amount, order.discount_amount), (70_000, 30_000))
        self.assertEqual([line.source for line in lines], ["code"])
        self.assertEqual(rule_row.used_count, 0)
        self.assertEqual(used, 0)

    async def test_early_renewal_rule_lowers_a_renewal_close_to_expiry(self) -> None:
        async with self.Session() as session:
            await self.add_rule(session, audience="early_renewal", audience_days=7, events="renew", percent_bps=1000)
            service = UserService(id=9, bot_user_id=1, plan_id=1, pg_username="u1",
                                  quota_expire_at=datetime.now(timezone.utc) + timedelta(days=2))
            session.add(service)
            await session.commit()
            plan = await session.get(Plan, 1)
            with patch("app.services.service_renewals.validate_renewal_selection", AsyncMock(return_value={})):
                order = await renew_service_with_plan(session, user_id=1, service=service, plan=plan)
        self.assertEqual(order.amount, 90_000)

    async def test_checkout_summary_names_the_rule_escaped(self) -> None:
        async with self.Session() as session:
            await self.add_rule(session, name="<b>یلدا</b>")
            order = await create_order(session, user_id=1, plan_id=1)
            summary = await checkout_summary(session, order, currency="تومان")
        self.assertIn("تخفیف خودکار (&lt;b&gt;یلدا&lt;/b&gt;)", summary)


class DiscountRuleMigrationTests(unittest.TestCase):
    def load(self, name: str):
        spec = importlib.util.spec_from_file_location(name, ROOT / f"alembic/versions/{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_upgrade_twice_and_downgrade_keeps_ledger_rows(self) -> None:
        ledger, rules = self.load("0127_order_discounts"), self.load("0128_discount_rules")
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY)"))
            conn.execute(text("INSERT INTO orders VALUES (7), (8)"))
            with Operations.context(MigrationContext.configure(conn)):
                ledger.upgrade()
                conn.execute(text("INSERT INTO order_discounts (order_id, user_id, source, amount) VALUES (7, 1, 'code', 5)"))
                rules.upgrade()
                rules.upgrade()
                insert = text("INSERT INTO order_discounts (order_id, user_id, source, amount, claim_key) "
                              "VALUES (:o, 1, 'rule', 10, 'first')")
                conn.execute(insert, {"o": 7})
                with self.assertRaises(sa.exc.IntegrityError), conn.begin_nested():
                    conn.execute(insert, {"o": 8})
                rules.downgrade()
                self.assertFalse(sa.inspect(conn).has_table("discount_rules"))
                self.assertEqual(conn.execute(text("SELECT count(*) FROM order_discounts")).scalar(), 2)
                rules.upgrade()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
