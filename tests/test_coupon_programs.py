"""Automatic personal coupons: issued once per event, inside the shop, usable at checkout."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.datastructures import FormData

from app.api.discount_rule_pages import program_from_form
from app.db.models import Base, BotUser, LoyaltyDiscountEntitlement, Order, OrderStatus, Plan
from app.services.coupon_programs import (
    TRIGGER_NEXT_PURCHASE,
    TRIGGER_REFERRAL_REWARD,
    TRIGGER_REFERRAL_WELCOME,
    ProgramInput,
    coupon_text,
    issue_after_delivery,
    issue_coupon,
    issue_referral_welcome,
    load_programs,
    save_program,
    validate_program,
)
from app.services.orders import _issue_referrer_coupon, apply_discount_to_order
from tests.postgres_support import scoped_async_engine, scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
SHOP_A, SHOP_B = 50, 60
PROGRAM = ProgramInput(enabled=True, percent=20, max_discount=None, min_purchase=0, valid_days=14, monthly_cap=None)


class ValidationTests(unittest.TestCase):
    def test_out_of_range_programs_are_refused(self) -> None:
        bad = [
            ("evil", PROGRAM),
            (TRIGGER_NEXT_PURCHASE, ProgramInput(True, 0, None, 0, 14, None)),
            (TRIGGER_NEXT_PURCHASE, ProgramInput(True, 95, None, 0, 14, None)),
            (TRIGGER_NEXT_PURCHASE, ProgramInput(True, True, None, 0, 14, None)),
            (TRIGGER_NEXT_PURCHASE, ProgramInput(True, 10, None, -1, 14, None)),
            (TRIGGER_NEXT_PURCHASE, ProgramInput(True, 10, None, 0, 0, None)),
            (TRIGGER_NEXT_PURCHASE, ProgramInput(True, 10, 0, 0, 14, None)),
            (TRIGGER_REFERRAL_REWARD, ProgramInput(True, 10, None, 0, 14, 5000)),
        ]
        for trigger, data in bad:
            with self.subTest(trigger=trigger, data=data), self.assertRaises(ValueError):
                validate_program(trigger, data)
        validate_program(TRIGGER_REFERRAL_REWARD, ProgramInput(True, 10, 50_000, 100_000, 30, 3))

    def test_form_parsing(self) -> None:
        data = program_from_form(FormData([("enabled", "1"), ("percent", "15"), ("valid_days", "7"),
                                           ("max_discount", ""), ("monthly_cap", "2")]))
        self.assertEqual(data, ProgramInput(True, 15, None, 0, 7, 2))
        with self.assertRaises(ValueError):
            program_from_form(FormData([("percent", "abc"), ("valid_days", "7")]))


class CouponIssueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([BotUser(id=s, telegram_id=s, referral_code=f"s{s}", role="reseller") for s in (SHOP_A, SHOP_B)])
            session.add_all([BotUser(id=1, telegram_id=101, referral_code="u1"),
                             BotUser(id=2, telegram_id=102, referral_code="u2", referred_by_id=1, referral_shop_id=SHOP_A),
                             BotUser(id=3, telegram_id=103, referral_code="u3", referred_by_id=1, referral_shop_id=SHOP_A)])
            await session.flush()
            session.add(Plan(id=1, name="پلن", price=100_000, duration_days=30, owner_reseller_id=SHOP_A))
            await session.commit()
        self.notify = patch("app.services.coupon_programs.notify_coupon", AsyncMock())
        self.sent = self.notify.start()

    async def asyncTearDown(self) -> None:
        self.notify.stop()
        await self.engine.dispose()

    async def coupons(self, session, user_id: int) -> list[LoyaltyDiscountEntitlement]:
        return list((await session.scalars(select(LoyaltyDiscountEntitlement).where(
            LoyaltyDiscountEntitlement.user_id == user_id).order_by(LoyaltyDiscountEntitlement.id))).all())

    async def delivered(self, session, *, user_id: int = 2, shop: int | None = SHOP_A, note: str | None = None,
                        amount: int = 100_000) -> Order:
        order = Order(user_id=user_id, plan_id=1, amount=amount, reseller_id=shop, note=note,
                      status=OrderStatus.DELIVERED.value)
        session.add(order)
        await session.commit()
        return order

    async def test_programs_are_per_shop(self) -> None:
        async with self.Session() as session:
            await save_program(session, SHOP_A, TRIGGER_NEXT_PURCHASE, PROGRAM)
            await save_program(session, SHOP_A, TRIGGER_NEXT_PURCHASE, ProgramInput(False, 5, None, 0, 3, None))
            a, b = await load_programs(session, SHOP_A), await load_programs(session, SHOP_B)
        self.assertEqual((a[TRIGGER_NEXT_PURCHASE].percent, a[TRIGGER_NEXT_PURCHASE].enabled), (5, False))
        self.assertEqual(b, {})

    async def test_next_purchase_coupon_is_issued_once_per_order_and_notified(self) -> None:
        async with self.Session() as session:
            await save_program(session, SHOP_A, TRIGGER_NEXT_PURCHASE, PROGRAM)
            order = await self.delivered(session)
            await issue_after_delivery(session, order)
            await issue_after_delivery(session, order)
            coupons = await self.coupons(session, 2)
        self.assertEqual([(c.percent, c.reseller_id, c.status) for c in coupons], [(20, SHOP_A, "available")])
        self.assertIsNotNone(coupons[0].expires_at)
        self.assertEqual(self.sent.await_count, 1)

    async def test_no_coupon_for_addons_free_orders_or_a_disabled_program(self) -> None:
        async with self.Session() as session:
            await save_program(session, SHOP_A, TRIGGER_NEXT_PURCHASE, PROGRAM)
            await issue_after_delivery(session, await self.delivered(session, note="svc_addon:1:2:volume:10"))
            await issue_after_delivery(session, await self.delivered(session, amount=0))
            await issue_after_delivery(session, await self.delivered(session, shop=SHOP_B))
            self.assertEqual(await self.coupons(session, 2), [])

    async def test_welcome_coupon_goes_to_the_referred_customer_in_the_referring_shop(self) -> None:
        async with self.Session() as session:
            await save_program(session, SHOP_A, TRIGGER_REFERRAL_WELCOME, PROGRAM)
            await save_program(session, SHOP_B, TRIGGER_REFERRAL_WELCOME, PROGRAM)
            await issue_referral_welcome(session, await session.get(BotUser, 2))
            await issue_referral_welcome(session, await session.get(BotUser, 2))
            await issue_referral_welcome(session, await session.get(BotUser, 1))
            self.assertEqual([c.reseller_id for c in await self.coupons(session, 2)], [SHOP_A])
            self.assertEqual(await self.coupons(session, 1), [])

    async def test_referrer_gets_one_coupon_per_friend_within_the_monthly_cap(self) -> None:
        async with self.Session() as session:
            await save_program(session, SHOP_A, TRIGGER_REFERRAL_REWARD,
                               ProgramInput(True, 10, None, 0, 30, 1))
            await _issue_referrer_coupon(session, await self.delivered(session, user_id=2))
            await _issue_referrer_coupon(session, await self.delivered(session, user_id=2))
            await _issue_referrer_coupon(session, await self.delivered(session, user_id=3))
            coupons = await self.coupons(session, 1)
        self.assertEqual([(c.reseller_id, c.issue_key) for c in coupons], [(SHOP_A, f"{TRIGGER_REFERRAL_REWARD}:{SHOP_A}:2")])

    async def test_purchase_in_another_shop_gives_the_referrer_nothing(self) -> None:
        async with self.Session() as session:
            await save_program(session, SHOP_B, TRIGGER_REFERRAL_REWARD, PROGRAM)
            await _issue_referrer_coupon(session, await self.delivered(session, user_id=2, shop=SHOP_B))
            self.assertEqual(await self.coupons(session, 1), [])

    async def test_coupon_discounts_an_order_of_its_shop_only(self) -> None:
        async with self.Session() as session:
            await save_program(session, SHOP_A, TRIGGER_NEXT_PURCHASE, PROGRAM)
            coupon = await issue_coupon(session, shop_id=SHOP_A, trigger=TRIGGER_NEXT_PURCHASE,
                                        user=await session.get(BotUser, 2), issue_key="k1")
            other = Order(user_id=2, plan_id=1, amount=100_000, reseller_id=SHOP_B, status=OrderStatus.PENDING.value)
            own = Order(user_id=2, plan_id=1, amount=100_000, reseller_id=SHOP_A, status=OrderStatus.PENDING.value)
            session.add_all([other, own])
            await session.commit()
            code, own_id = coupon.code, own.id
            with self.assertRaises(ValueError):
                await apply_discount_to_order(session, other, code)
            await session.rollback()
            own = await apply_discount_to_order(session, await session.get(Order, own_id), code)
        self.assertEqual(own.amount, 80_000)

    async def test_coupon_of_another_customer_is_refused(self) -> None:
        async with self.Session() as session:
            await save_program(session, SHOP_A, TRIGGER_NEXT_PURCHASE, PROGRAM)
            coupon = await issue_coupon(session, shop_id=SHOP_A, trigger=TRIGGER_NEXT_PURCHASE,
                                        user=await session.get(BotUser, 2), issue_key="k2")
            order = Order(user_id=3, plan_id=1, amount=100_000, reseller_id=SHOP_A, status=OrderStatus.PENDING.value)
            session.add(order)
            await session.commit()
            with self.assertRaises(ValueError):
                await apply_discount_to_order(session, order, coupon.code)

    async def test_notice_text_names_the_program(self) -> None:
        async with self.Session() as session:
            await save_program(session, SHOP_A, TRIGGER_NEXT_PURCHASE,
                               ProgramInput(True, 15, 30_000, 50_000, 7, None))
            coupon = await issue_coupon(session, shop_id=SHOP_A, trigger=TRIGGER_NEXT_PURCHASE,
                                        user=await session.get(BotUser, 2), issue_key="k3")
        message = coupon_text(coupon, TRIGGER_NEXT_PURCHASE)
        self.assertIn("کوپن خرید بعدی", message)
        self.assertIn("15٪", message)
        self.assertIn("7 روز", message)

    async def test_failed_issue_keeps_the_order_readable(self) -> None:
        async with self.Session() as session:
            await save_program(session, SHOP_A, TRIGGER_NEXT_PURCHASE, PROGRAM)
            order = await self.delivered(session)
            for error in (sa.exc.OperationalError("x", {}, None), TypeError("unexpected")):
                with patch("app.services.coupon_programs.issue_coupon", AsyncMock(side_effect=error)):
                    await issue_after_delivery(session, order)
            self.assertEqual(order.status, OrderStatus.DELIVERED.value)
            count = await session.scalar(select(func.count()).select_from(LoyaltyDiscountEntitlement))
        self.assertEqual(count, 0)


class CouponMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keeps_entitlements(self) -> None:
        spec = importlib.util.spec_from_file_location("coupons", ROOT / "alembic/versions/0129_coupon_programs.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE loyalty_discount_entitlements (id INTEGER PRIMARY KEY, code VARCHAR(64))"))
            conn.execute(text("INSERT INTO loyalty_discount_entitlements VALUES (1, 'LOYOLD')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                insert = text("INSERT INTO loyalty_discount_entitlements (id, code, issue_key) VALUES (:i, :c, 'k')")
                conn.execute(insert, {"i": 2, "c": "LOYA"})
                with self.assertRaises(sa.exc.IntegrityError), conn.begin_nested():
                    conn.execute(insert, {"i": 3, "c": "LOYB"})
                conn.execute(text("INSERT INTO loyalty_discount_entitlements (id, code) VALUES (4, 'LOYC')"))
                migration.downgrade()
                self.assertFalse(sa.inspect(conn).has_table("coupon_programs"))
                self.assertEqual(conn.execute(text("SELECT count(*) FROM loyalty_discount_entitlements")).scalar(), 3)
                migration.upgrade()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
