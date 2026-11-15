"""Monthly statements, oldest-first payment, the overdue ladder and write-off."""

from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import (
    Base,
    BotUser,
    CreditStatement,
    ResellerBillingTransaction,
    ResellerPlan,
    ResellerProfile,
    ShopCreditAccount,
)
from app.services.billing import credit_topup
from app.services.credit_cycle import run_credit_cycle
from app.services.credit_lift import lift_paid_restriction
from app.services.credit_statements import (
    LadderDays,
    LadderStep,
    ladder_step,
    previous_month,
    refusal_for,
    write_off_statement,
)
from app.services.payg_gate import charge_shop_change
from app.services.payg_pricing import GB, ChargeEvent, ServiceChange
from app.services.provision_gate import ProvisionError
from app.services.shop_credit import CreditLimitChange, set_credit_limit

ROOT = Path(__file__).resolve().parents[1]
OCT_15 = datetime(2026, 10, 15, tzinfo=timezone.utc)
NOV_1 = datetime(2026, 11, 1, tzinfo=timezone.utc)
DAYS = LadderDays(due=7, block_create=3, block_changes=7, block=14)


class PureTests(unittest.TestCase):
    def test_previous_month(self) -> None:
        self.assertEqual(previous_month(datetime(2026, 11, 8, 13, tzinfo=timezone.utc)), (datetime(2026, 10, 1, tzinfo=timezone.utc), NOV_1))
        self.assertEqual(previous_month(datetime(2027, 1, 3, tzinfo=timezone.utc))[0], datetime(2026, 12, 1, tzinfo=timezone.utc))

    def test_ladder_steps(self) -> None:
        due = NOV_1 + timedelta(days=7)
        cases = {-1: LadderStep.NONE, 0: LadderStep.OVERDUE, 3: LadderStep.NO_CREATE, 7: LadderStep.NO_CHANGES,
                 14: LadderStep.BLOCKED}
        for late, step in cases.items():
            self.assertEqual(ladder_step(now=due + timedelta(days=late), due_at=due, days=DAYS), step)

    def test_refusals(self) -> None:
        self.assertIsNone(refusal_for(LadderStep.OVERDUE, ChargeEvent.CREATE))
        self.assertIsNotNone(refusal_for(LadderStep.NO_CREATE, ChargeEvent.CREATE))
        self.assertIsNone(refusal_for(LadderStep.NO_CREATE, ChargeEvent.RENEW))
        for event in ChargeEvent:
            self.assertIsNotNone(refusal_for(LadderStep.NO_CHANGES, event))


class StatementCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=5, telegram_id=5, referral_code="a", role="reseller", wallet_balance=-15_000),
                BotUser(id=6, telegram_id=6, referral_code="b", role="reseller", wallet_balance=-9_000),
                ResellerPlan(id=1, name="payg", billing_mode="payg", price_per_gb=1000,
                             payg_basis="traffic", payg_charge_upfront=True),
                ResellerProfile(id=1, user_id=5, plan_id=1, billing_mode="payg", payg_wallet_linked=True),
                ResellerProfile(id=2, user_id=6, plan_id=1, billing_mode="payg", payg_wallet_linked=True),
                ResellerBillingTransaction(reseller_user_id=5, kind="charge", amount=-15_000, balance_after=-15_000,
                                           idempotency_key="payg:5:order:1#1", created_at=OCT_15),
            ])
            await session.commit()
            for shop in (5, 6):
                await set_credit_limit(session, shop, CreditLimitChange(limit=50_000), actor="owner")
        for target in ("app.services.credit_cycle.notify_statement", "app.services.credit_cycle.suspend_payg_reseller",
                       "app.services.credit_cycle.restore_payg_reseller"):
            mock = patch(target, AsyncMock())
            setattr(self, target.rsplit(".", 1)[1], mock.start())
            self.addCleanup(mock.stop)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def cycle(self, at: datetime) -> None:
        async with self.Session() as session:
            await run_credit_cycle(session, now=at)

    async def statements(self, shop: int = 5) -> list[CreditStatement]:
        async with self.Session() as session:
            return list((await session.scalars(select(CreditStatement).where(CreditStatement.shop_id == shop))).all())

    async def account(self, shop: int = 5) -> ShopCreditAccount:
        async with self.Session() as session:
            return await session.scalar(select(ShopCreditAccount).where(ShopCreditAccount.shop_id == shop))

    async def set_balance(self, value: int, shop: int = 5) -> None:
        async with self.Session() as session:
            (await session.get(BotUser, shop)).wallet_balance = value
            await session.commit()

    async def charge(self, event: ChargeEvent, key: str, *, exempt: bool = False):
        async with self.Session() as session:
            return await charge_shop_change(
                session, shop_id=5, change=ServiceChange(event, data_limit_bytes=GB, days=30), request_key=key,
                pg_username="c", ladder_exempt=exempt,
            )


class StatementTests(StatementCase):
    async def test_a_month_closes_once_with_its_debt(self) -> None:
        await self.cycle(NOV_1 + timedelta(hours=2))
        await self.cycle(NOV_1 + timedelta(hours=3))
        [statement] = await self.statements()
        self.assertEqual((statement.amount_due, statement.charges_total, statement.status), (15_000, 15_000, "issued"))
        self.assertEqual(statement.due_at.replace(tzinfo=timezone.utc), NOV_1 + timedelta(days=7))
        self.notify_statement.assert_awaited()
        self.assertEqual(self.notify_statement.await_args_list[0].kwargs["notice"], 1)

    async def test_payment_clears_the_oldest_debt_even_with_new_spending(self) -> None:
        await self.cycle(NOV_1 + timedelta(hours=2))
        # Paid the October debt, then spent 20,000 more in November.
        async with self.Session() as session:
            session.add(ResellerBillingTransaction(
                reseller_user_id=5, kind="charge", amount=-20_000, balance_after=-20_000,
                idempotency_key="payg:5:order:2#1", created_at=NOV_1 + timedelta(days=2)))
            await session.commit()
        await self.set_balance(-20_000)
        await self.cycle(NOV_1 + timedelta(days=3))
        [statement] = await self.statements()
        self.assertEqual(statement.status, "paid")

    async def test_the_ladder_climbs_and_payment_lifts_it(self) -> None:
        await self.cycle(NOV_1 + timedelta(hours=2))
        due = NOV_1 + timedelta(days=7)
        await self.cycle(due + timedelta(hours=1))
        self.assertEqual(((await self.account()).restriction, (await self.account()).status), (1, "overdue"))
        await self.cycle(due + timedelta(days=4))
        self.assertEqual((await self.account()).restriction, LadderStep.NO_CREATE)
        with self.assertRaises(ProvisionError):
            await self.charge(ChargeEvent.CREATE, "order:300")
        await self.cycle(due + timedelta(days=15))
        self.assertEqual(((await self.account()).restriction, (await self.account()).status), (4, "blocked"))
        # Both shops owe (shop 6 carries 9,000 from the start); each is suspended once.
        suspended = sorted(int(c.args[1].user_id) for c in self.suspend_payg_reseller.await_args_list)
        self.assertEqual(suspended, [5, 6])
        await self.set_balance(1_000)
        await self.cycle(due + timedelta(days=16))
        account = await self.account()
        self.assertEqual((account.restriction, account.status), (0, "active"))
        self.assertEqual([s.status for s in await self.statements()], ["paid"])
        # Each notice once: issue, then one per ladder step reached.
        for shop in (5, 6):
            notices = [c.kwargs["notice"] for c in self.notify_statement.await_args_list if int(c.args[1].user_id) == shop]
            self.assertEqual(notices, sorted(set(notices)))
            self.assertIn(5, notices)  # the «blocked» notice

    async def test_panel_changes_are_still_charged_while_restricted(self) -> None:
        await self.cycle(NOV_1 + timedelta(hours=2))
        await self.cycle(NOV_1 + timedelta(days=7 + 8))
        await self.set_balance(10_000)
        self.assertIsNotNone(await self.charge(ChargeEvent.ADD_VOLUME, "recon:1:0:0", exempt=True))

    async def test_write_off_forgives_only_the_unpaid_part_once(self) -> None:
        await self.cycle(NOV_1 + timedelta(hours=2))
        await self.set_balance(-6_000)  # 9,000 of the 15,000 already paid
        [statement] = await self.statements()
        async with self.Session() as session:
            self.assertTrue(await write_off_statement(session, statement.id, actor="owner", now=NOV_1))
            self.assertFalse(await write_off_statement(session, statement.id, actor="owner", now=NOV_1))
            self.assertEqual(int((await session.get(BotUser, 5)).wallet_balance), 0)
            self.assertEqual(int((await session.get(BotUser, 6)).wallet_balance), -9_000)


class ImmediateLiftTests(StatementCase):
    async def block(self) -> None:
        await self.cycle(NOV_1 + timedelta(hours=2))
        await self.cycle(NOV_1 + timedelta(days=22))
        self.assertEqual((await self.account()).restriction, LadderStep.BLOCKED)

    async def test_a_payment_lifts_the_ladder_right_after_it_commits(self) -> None:
        await self.block()
        with patch("app.services.credit_lift.schedule_lift") as schedule:
            async with self.Session() as session:
                await credit_topup(session, 5, 20_000, idempotency_key="topup:lift:1")
        schedule.assert_called_once_with(5)
        async with self.Session() as session:
            self.assertEqual(await lift_paid_restriction(session, 5, now=NOV_1 + timedelta(days=22)), LadderStep.NONE)
        self.assertEqual((await self.account()).status, "active")

    async def test_a_rolled_back_or_unrestricted_credit_schedules_nothing(self) -> None:
        with patch("app.services.credit_lift.schedule_lift") as schedule:
            async with self.Session() as session:
                await credit_topup(session, 6, 1_000, idempotency_key="topup:lift:2")  # shop 6 is not restricted
            await self.block()
            async with self.Session() as session:
                await credit_topup(session, 5, 1_000, idempotency_key="topup:lift:3", commit=False)
                await session.rollback()
        schedule.assert_not_called()

    async def test_a_partial_payment_keeps_the_restriction(self) -> None:
        await self.block()
        await self.set_balance(-1_000)
        async with self.Session() as session:
            self.assertEqual(await lift_paid_restriction(session, 5, now=NOV_1 + timedelta(days=22)), LadderStep.BLOCKED)


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade(self) -> None:
        path = ROOT / "alembic/versions/0100_credit_statements.py"
        spec = importlib.util.spec_from_file_location("credit_statements_migration", path)
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
            self.assertTrue(sa.inspect(conn).has_table("credit_statements"))
            with Operations.context(MigrationContext.configure(conn)):
                module.downgrade()
            self.assertFalse(sa.inspect(conn).has_table("credit_statements"))


if __name__ == "__main__":
    unittest.main()
