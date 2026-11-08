"""PAYG charge ledger: money moves once per confirmed change, only within its own shop."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, ResellerBillingTransaction, ResellerCharge, ResellerPlan, ResellerProfile
from app.services.payg_charges import (
    ChargeRequest,
    ChargeStatus,
    list_review_charges,
    panel_target_of,
    refund_charge,
    reserve_charge,
    send_to_review,
    settle_charge,
)
from app.services.payg_pricing import GB, ChargeEvent, PaygPriceError, ServiceChange

ROOT = Path(__file__).resolve().parents[1]
KEY = "a1b2c3d4e5f60718"


def create(gb: int = 10, *, key: str = KEY, expected: int | None = None) -> ChargeRequest:
    return ChargeRequest(
        request_key=key,
        change=ServiceChange(ChargeEvent.CREATE, data_limit_bytes=gb * GB, days=30),
        pg_username="cust_1",
        panel_target={"username": "cust_1", "data_limit": gb * GB},
        expected_amount=expected,
    )


class LedgerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'c.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                ResellerPlan(id=1, name="payg", billing_mode="payg", price_per_gb=1000,
                             payg_basis="traffic", payg_charge_upfront=True),
                ResellerPlan(id=2, name="fixed", billing_mode="fixed"),
                BotUser(id=5, telegram_id=5, referral_code="r5", role="reseller", wallet_balance=50_000),
                BotUser(id=6, telegram_id=6, referral_code="r6", role="reseller", wallet_balance=50_000),
                BotUser(id=7, telegram_id=7, referral_code="r7", role="reseller", wallet_balance=50_000),
            ])
            session.add_all([
                ResellerProfile(id=1, user_id=5, plan_id=1, billing_mode="payg", payg_wallet_linked=True),
                ResellerProfile(id=2, user_id=6, plan_id=1, billing_mode="payg", payg_wallet_linked=True),
                ResellerProfile(id=3, user_id=7, plan_id=2, billing_mode="fixed"),
            ])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def reserve(self, request: ChargeRequest, shop: int = 1) -> ResellerCharge | None:
        async with self.Session() as session:
            profile = await session.get(ResellerProfile, shop)
            return await reserve_charge(session, profile, request)

    async def wallet(self, user_id: int) -> int:
        async with self.Session() as session:
            return int((await session.get(BotUser, user_id)).wallet_balance)

    async def count(self, model) -> int:
        async with self.Session() as session:
            return int(await session.scalar(select(func.count()).select_from(model)))


class ReserveTests(LedgerCase):
    async def test_charge_is_taken_before_the_panel_call_with_its_target(self) -> None:
        charge = await self.reserve(create(10))
        self.assertEqual(charge.status, ChargeStatus.PENDING)
        self.assertEqual(charge.amount, 10_000)
        self.assertEqual(panel_target_of(charge), {"username": "cust_1", "data_limit": 10 * GB})
        self.assertEqual(await self.wallet(5), 40_000)
        self.assertEqual(await self.wallet(6), 50_000)

    async def test_repeated_confirmation_charges_once(self) -> None:
        first = await self.reserve(create(10))
        again = await self.reserve(create(10))
        self.assertEqual(first.id, again.id)
        self.assertEqual(await self.wallet(5), 40_000)
        self.assertEqual(await self.count(ResellerBillingTransaction), 1)

    async def test_concurrent_confirmations_charge_once(self) -> None:
        results = await asyncio.gather(self.reserve(create(10)), self.reserve(create(10)), return_exceptions=True)
        charges = [r for r in results if isinstance(r, ResellerCharge)]
        self.assertTrue(charges)
        self.assertEqual(len({c.id for c in charges}), 1)
        self.assertEqual(await self.wallet(5), 40_000)
        self.assertEqual(await self.count(ResellerCharge), 1)

    async def test_shortfall_charges_nothing_and_records_nothing(self) -> None:
        with self.assertRaises(ValueError):
            await self.reserve(create(51))
        self.assertEqual(await self.wallet(5), 50_000)
        self.assertEqual(await self.count(ResellerCharge), 0)

    async def test_same_client_key_in_another_shop_is_its_own_charge(self) -> None:
        await self.reserve(create(10))
        # Keys are scoped per shop: the same client key is a separate charge for shop 2.
        other = await self.reserve(create(10), shop=2)
        self.assertNotEqual(other.shop_id, 5)
        self.assertEqual(await self.wallet(6), 40_000)
        self.assertEqual(await self.wallet(5), 40_000)

    async def test_fixed_package_shop_is_never_charged(self) -> None:
        self.assertIsNone(await self.reserve(create(10), shop=3))
        self.assertEqual(await self.wallet(7), 50_000)

    async def test_price_changed_since_shown_is_refused(self) -> None:
        with self.assertRaises(PaygPriceError):
            await self.reserve(create(10, expected=9_000))
        self.assertEqual(await self.wallet(5), 50_000)

    async def test_hostile_request_key_is_refused(self) -> None:
        for key in ("short", "x" * 65, "bad key!", "a1b2c3d4'; --"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                await self.reserve(create(10, key=key))


class TransitionTests(LedgerCase):
    async def test_settling_twice_keeps_the_money_taken_once(self) -> None:
        charge = await self.reserve(create(10))
        async with self.Session() as session:
            self.assertTrue(await settle_charge(session, charge))
            self.assertFalse(await settle_charge(session, charge))
        self.assertEqual(await self.wallet(5), 40_000)

    async def test_refund_returns_the_money_once(self) -> None:
        charge = await self.reserve(create(10))
        async with self.Session() as session:
            self.assertTrue(await refund_charge(session, charge, reason="panel refused"))
            self.assertFalse(await refund_charge(session, charge, reason="panel refused"))
        self.assertEqual(await self.wallet(5), 50_000)

    async def test_concurrent_refunds_return_the_money_once(self) -> None:
        charge = await self.reserve(create(10))

        async def refund() -> bool:
            async with self.Session() as session:
                return await refund_charge(session, charge, reason="refused")

        results = await asyncio.gather(refund(), refund(), return_exceptions=True)
        self.assertEqual([r for r in results if r is True], [True])
        self.assertEqual(await self.wallet(5), 50_000)

    async def test_settled_charge_cannot_be_refunded(self) -> None:
        charge = await self.reserve(create(10))
        async with self.Session() as session:
            await settle_charge(session, charge)
            self.assertFalse(await refund_charge(session, charge, reason="late"))
        self.assertEqual(await self.wallet(5), 40_000)

    async def test_unknown_outcome_keeps_the_money_until_resolved(self) -> None:
        charge = await self.reserve(create(10))
        async with self.Session() as session:
            self.assertTrue(await send_to_review(session, charge))
            self.assertEqual(await self.wallet(5), 40_000)
            mine = await list_review_charges(session, shop_id=5)
            others = await list_review_charges(session, shop_id=6)
            self.assertEqual([c.id for c in mine], [charge.id])
            self.assertEqual(others, [])
            self.assertTrue(await settle_charge(session, charge))
            self.assertEqual(await list_review_charges(session, shop_id=None), [])


def _load_migration():
    path = ROOT / "alembic/versions/0097_payg_charges.py"
    spec = importlib.util.spec_from_file_location("payg_charges_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.migration = _load_migration()
        self.engine = sa.create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
            conn.execute(sa.text("CREATE TABLE reseller_plans (id INTEGER PRIMARY KEY, billing_mode TEXT)"))
            conn.execute(sa.text("INSERT INTO reseller_plans (id, billing_mode) VALUES (1, 'payg')"))

    def _run(self, conn, *, down: bool = False) -> None:
        with Operations.context(MigrationContext.configure(conn)):
            (self.migration.downgrade if down else self.migration.upgrade)()

    def test_existing_payg_plans_keep_usage_billing_and_downgrade_keeps_rows(self) -> None:
        with self.engine.begin() as conn:
            self._run(conn)
            self._run(conn)
            row = conn.execute(sa.text(
                "SELECT payg_basis, payg_charge_upfront, payg_user_price FROM reseller_plans WHERE id=1"
            )).one()
            self.assertEqual(tuple(row), ("traffic", 0, 0))
            self.assertTrue(sa.inspect(conn).has_table("reseller_charges"))
            indexes = {i["name"] for i in sa.inspect(conn).get_indexes("reseller_charges")}
            self.assertIn("ix_reseller_charges_status", indexes)
            self._run(conn, down=True)
            self.assertFalse(sa.inspect(conn).has_table("reseller_charges"))
            cols = {c["name"] for c in sa.inspect(conn).get_columns("reseller_plans")}
            self.assertNotIn("payg_basis", cols)
            self.assertEqual(conn.execute(sa.text("SELECT billing_mode FROM reseller_plans")).scalar(), "payg")


if __name__ == "__main__":
    unittest.main()
