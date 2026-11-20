"""Tiered cashback on wallet top-ups: one grant per payment, inside its shop, reversed on void."""

from __future__ import annotations

import asyncio
import importlib.util
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Base, BotUser, CashbackGrant, CashbackRule, Payment, PaymentStatus
from app.services.cashback import credited_text, mature_grants
from app.services.cashback_topup import grant_after_topup, grant_for_topup, reverse_after_topup_void
from app.services.users import set_setting
from app.services.wallet import get_wallet_balance
from tests.postgres_support import scoped_async_engine, scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
SHOP_A, SHOP_B, BUYER = 5, 6, 9


class TopupCashbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP_A, telegram_id=5, referral_code="a", role="reseller"),
                BotUser(id=SHOP_B, telegram_id=6, referral_code="b", role="reseller"),
                BotUser(id=BUYER, telegram_id=9, referral_code="c"),
                CashbackRule(id=1, shop_id=SHOP_A, name="3%", percent_bps=300, min_order_amount=500_000, events="topup"),
                CashbackRule(id=2, shop_id=SHOP_A, name="6%", percent_bps=600, min_order_amount=1_000_000, events="topup"),
                CashbackRule(id=3, shop_id=SHOP_A, name="purchases", percent_bps=5000),
                CashbackRule(id=4, shop_id=SHOP_B, name="b", percent_bps=5000, events="topup"),
            ])
            await session.commit()
            await set_setting(session, "cashback_enabled", "1", reseller_id=SHOP_A)
            await set_setting(session, "cashback_hold_hours", "0", reseller_id=SHOP_A)
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def topup(self, amount: int, *, shop: int | None = SHOP_A, status: str = PaymentStatus.APPROVED.value) -> Payment:
        async with self.Session() as session:
            payment = Payment(user_id=BUYER, amount=amount, method="card", status=status,
                              is_wallet_topup=True, wallet_shop_id=shop)
            session.add(payment)
            await session.commit()
            return payment

    async def grant(self, payment: Payment) -> CashbackGrant | None:
        async with self.Session() as session:
            return await grant_for_topup(session, await session.get(Payment, payment.id), now=NOW)

    async def grants(self) -> list[CashbackGrant]:
        async with self.Session() as session:
            return list(await session.scalars(select(CashbackGrant).order_by(CashbackGrant.id)))

    async def test_the_biggest_tier_that_applies_wins(self) -> None:
        small, middle, big = await self.topup(100_000), await self.topup(600_000), await self.topup(1_200_000)
        self.assertIsNone(await self.grant(small))
        self.assertEqual((await self.grant(middle)).amount, 18_000)
        self.assertEqual((await self.grant(big)).amount, 72_000)
        self.assertEqual([(g.payment_id, g.order_id, g.rule_id) for g in await self.grants()],
                         [(middle.id, None, 1), (big.id, None, 2)])

    async def test_a_purchase_rule_never_pays_on_a_top_up(self) -> None:
        async with self.Session() as session:
            await session.execute(sa.delete(CashbackRule).where(CashbackRule.id.in_([1, 2])))
            await session.commit()
        self.assertIsNone(await self.grant(await self.topup(2_000_000)))

    async def test_repeated_and_concurrent_approval_grant_once(self) -> None:
        payment = await self.topup(1_000_000)
        await asyncio.gather(self.grant(payment), self.grant(payment))
        await self.grant(payment)
        self.assertEqual(len(await self.grants()), 1)

    async def test_pending_or_other_shop_or_disabled_cashback_earns_nothing(self) -> None:
        self.assertIsNone(await self.grant(await self.topup(1_000_000, status=PaymentStatus.PENDING.value)))
        # Shop B has a top-up rule but cashback is off there; shop A's rules never apply to it.
        self.assertIsNone(await self.grant(await self.topup(1_000_000, shop=SHOP_B)))
        self.assertIsNone(await self.grant(await self.topup(1_000_000, shop=None)))
        self.assertEqual(await self.grants(), [])

    async def test_the_bonus_matures_into_the_same_shop_wallet(self) -> None:
        payment = await self.topup(1_000_000)
        await self.grant(payment)
        async with self.Session() as session:
            self.assertEqual(await mature_grants(session, now=NOW + timedelta(minutes=1)), 1)
            self.assertEqual(await get_wallet_balance(session, BUYER, shop_id=SHOP_A), 60_000)
            self.assertEqual(await get_wallet_balance(session, BUYER, shop_id=SHOP_B), 0)
        self.assertIn(f"شارژ کیف پول #{payment.id}", credited_text((await self.grants())[0]))

    async def test_void_cancels_a_pending_bonus_and_takes_back_a_credited_one(self) -> None:
        pending, credited = await self.topup(1_000_000), await self.topup(500_000)
        await self.grant(credited)
        async with self.Session() as session:
            await mature_grants(session, now=NOW + timedelta(minutes=1))
        await self.grant(pending)
        async with self.Session() as session:
            await reverse_after_topup_void(session, pending.id, reason="void")
            await reverse_after_topup_void(session, credited.id, reason="void")
            await reverse_after_topup_void(session, credited.id, reason="void")
            self.assertEqual(await get_wallet_balance(session, BUYER, shop_id=SHOP_A), 0)
        statuses = {g.payment_id: (g.status, g.reversed_amount) for g in await self.grants()}
        self.assertEqual(statuses, {pending.id: ("cancelled", 0), credited.id: ("reversed", 15_000)})

    async def test_a_failure_never_raises_into_the_credited_top_up(self) -> None:
        payment = await self.topup(1_000_000)
        async with self.Session() as session:
            with unittest.mock.patch(
                "app.services.cashback_topup.grant_for_topup", side_effect=sa.exc.OperationalError("x", {}, Exception())
            ):
                await grant_after_topup(session, payment.id)
            await grant_after_topup(session, payment.id)
        self.assertEqual([g.amount for g in await self.grants()], [60_000])

    async def test_first_top_up_rule_applies_only_once_per_shop(self) -> None:
        async with self.Session() as session:
            session.add(CashbackRule(id=5, shop_id=SHOP_A, name="first", mode="fixed", fixed_amount=90_000,
                                     events="topup", first_purchase_only=True))
            await session.commit()
        first, second = await self.topup(100_000), await self.topup(100_000)
        self.assertEqual((await self.grant(first)).amount, 90_000)
        self.assertIsNone(await self.grant(second))


    async def test_resuming_an_approved_top_up_grants_its_bonus_once(self) -> None:
        from app.services import orders

        payment = await self.topup(1_000_000)
        for _ in range(2):
            async with self.Session() as session:
                await orders._resume_approved_payment(session, await session.get(Payment, payment.id))
        async with self.Session() as session:
            self.assertEqual(await get_wallet_balance(session, BUYER, shop_id=SHOP_A), 1_000_000)
        self.assertEqual([(g.payment_id, g.amount) for g in await self.grants()], [(payment.id, 60_000)])


class TopupCashbackMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_the_rows(self) -> None:
        spec = importlib.util.spec_from_file_location("topup", ROOT / "alembic/versions/0124_cashback_topup_grants.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE cashback_grants (id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL)"))
            conn.execute(text("INSERT INTO cashback_grants VALUES (1, 10)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                conn.execute(text("INSERT INTO cashback_grants (id, order_id, payment_id) VALUES (2, NULL, 7)"))
                with self.assertRaises(sa.exc.IntegrityError):
                    with conn.begin_nested():
                        conn.execute(text("INSERT INTO cashback_grants (id, order_id, payment_id) VALUES (3, NULL, 7)"))
                migration.downgrade()
                self.assertEqual(conn.execute(text("SELECT count(*) FROM cashback_grants")).scalar(), 2)
                self.assertNotIn("payment_id", {c["name"] for c in sa.inspect(conn).get_columns("cashback_grants")})
                migration.upgrade()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
