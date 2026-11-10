"""Credit lines: PAYG charges may use credit, nothing else may, and every change is audited."""

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.shop_credit_pages import credit_change_from_form
from app.db.models import Base, BotUser, CreditEvent, ResellerPlan, ResellerProfile, ShopCreditAccount
from app.services.billing import (
    assert_billing_allows_provision,
    debit_adjustment,
    ensure_billing_enabled,
    payg_available_balance,
)
from app.services.payg_charges import ChargeRequest, reserve_charge
from app.services.payg_pricing import GB, ChargeEvent, ServiceChange
from app.services.shop_credit import (
    CreditLimitChange,
    CreditStatus,
    claim_credit_alert,
    effective_limit,
    set_credit_frozen,
    set_credit_limit,
    set_credit_status,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)


def request(gb: int, key: str) -> ChargeRequest:
    return ChargeRequest(request_key=key, change=ServiceChange(ChargeEvent.CREATE, data_limit_bytes=gb * GB, days=30),
                         pg_username="c")


class LimitTests(unittest.TestCase):
    def account(self, **kw) -> SimpleNamespace:
        base = dict(status="active", credit_limit=10_000, temp_limit=None, temp_limit_until=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_only_an_active_line_lends(self) -> None:
        self.assertEqual(effective_limit(None, now=NOW), 0)
        self.assertEqual(effective_limit(self.account(), now=NOW), 10_000)
        for status in ("frozen", "overdue", "blocked"):
            self.assertEqual(effective_limit(self.account(status=status), now=NOW), 0)

    def test_a_temporary_limit_applies_until_it_ends(self) -> None:
        later = NOW + timedelta(days=3)
        self.assertEqual(effective_limit(self.account(temp_limit=50_000, temp_limit_until=later), now=NOW), 50_000)
        self.assertEqual(effective_limit(self.account(temp_limit=50_000, temp_limit_until=later), now=later), 10_000)
        self.assertEqual(effective_limit(self.account(temp_limit=5_000, temp_limit_until=later), now=NOW), 10_000)

    def test_form_parsing(self) -> None:
        change = credit_change_from_form({"credit_limit": "20000", "temp_limit": "50000", "temp_limit_days": "7"}, now=NOW)
        self.assertEqual((change.limit, change.temp_limit, change.temp_limit_until), (20000, 50000, NOW + timedelta(days=7)))
        for bad in ({"credit_limit": "abc"}, {"credit_limit": "1", "temp_limit": "5"},
                    {"credit_limit": "1", "temp_limit": "5", "temp_limit_days": "999"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                credit_change_from_form(bad, now=NOW)


class CreditCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'c.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=5, telegram_id=5, referral_code="a", role="reseller", wallet_balance=5_000),
                BotUser(id=6, telegram_id=6, referral_code="b", role="reseller", wallet_balance=5_000),
                ResellerPlan(id=1, name="payg", billing_mode="payg", price_per_gb=1000,
                             payg_basis="traffic", payg_charge_upfront=True),
            ])
            session.add_all([
                ResellerProfile(id=1, user_id=5, plan_id=1, billing_mode="payg", payg_wallet_linked=True),
                ResellerProfile(id=2, user_id=6, plan_id=1, billing_mode="payg", payg_wallet_linked=True),
            ])
            await session.commit()
            await set_credit_limit(session, 5, CreditLimitChange(limit=20_000), actor="owner")

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def wallet(self, uid: int = 5) -> int:
        async with self.Session() as session:
            return int((await session.get(BotUser, uid)).wallet_balance)

    async def reserve(self, gb: int, key: str, shop: int = 1):
        async with self.Session() as session:
            return await reserve_charge(session, await session.get(ResellerProfile, shop), request(gb, key))


class SpendingTests(CreditCase):
    async def test_charges_may_use_credit_down_to_the_limit(self) -> None:
        await self.reserve(15, "order:101")
        self.assertEqual(await self.wallet(), -10_000)
        with self.assertRaises(ValueError):
            await self.reserve(15, "order:102")  # would reach -25,000, past the 20,000 line
        await self.reserve(10, "order:103")
        self.assertEqual(await self.wallet(), -20_000)
        with self.assertRaises(ValueError):
            await self.reserve(1, "order:104")
        self.assertEqual(await self.wallet(), -20_000)

    async def test_a_shop_without_a_line_cannot_go_negative(self) -> None:
        with self.assertRaises(ValueError):
            await self.reserve(6, "order:9", shop=2)
        self.assertEqual(await self.wallet(6), 5_000)

    async def test_manual_owner_decreases_never_use_credit(self) -> None:
        async with self.Session() as session:
            with self.assertRaises(ValueError):
                await debit_adjustment(session, 5, 10_000, idempotency_key="adj-1")
        self.assertEqual(await self.wallet(), 5_000)

    async def test_a_frozen_line_lends_nothing_new(self) -> None:
        async with self.Session() as session:
            await set_credit_status(session, 5, CreditStatus.FROZEN, actor="owner")
        with self.assertRaises(ValueError):
            await self.reserve(6, "order:4")
        self.assertEqual(await self.wallet(), 5_000)

    async def test_the_freeze_switch_never_overrides_the_overdue_ladder(self) -> None:
        async with self.Session() as session:
            await set_credit_status(session, 5, CreditStatus.OVERDUE, actor="ladder")
            for frozen in (True, False):  # a saved form keeps the ladder's status either way
                account = await set_credit_frozen(session, 5, frozen, actor="owner")
                self.assertEqual(account.status, CreditStatus.OVERDUE)
            await set_credit_status(session, 5, CreditStatus.ACTIVE, actor="ladder")
            self.assertEqual((await set_credit_frozen(session, 5, True, actor="owner")).status, CreditStatus.FROZEN)
            self.assertEqual((await set_credit_frozen(session, 5, False, actor="owner")).status, CreditStatus.ACTIVE)

    async def test_racing_charges_never_pass_the_floor(self) -> None:
        results = await asyncio.gather(
            *(self.reserve(10, f"order:r{i}") for i in range(4)), return_exceptions=True
        )
        taken = [r for r in results if not isinstance(r, BaseException)]
        self.assertEqual(len(taken), 2)  # 5,000 + 20,000 covers two 10,000 charges
        self.assertGreaterEqual(await self.wallet(), -20_000)

    async def test_a_shop_inside_its_line_is_not_treated_as_empty(self) -> None:
        await self.reserve(15, "order:105")
        async with self.Session() as session:
            await ensure_billing_enabled(session)
            profile = await session.get(ResellerProfile, 1)
            self.assertEqual(await payg_available_balance(session, profile), 10_000)
            with patch("app.services.billing_suspend.suspend_payg_reseller", AsyncMock()) as suspend:
                await assert_billing_allows_provision(session, reseller_user_id=5)
            suspend.assert_not_awaited()


class AuditAndAlertTests(CreditCase):
    async def test_changes_are_audited_and_validated_per_shop(self) -> None:
        async with self.Session() as session:
            await set_credit_limit(session, 5, CreditLimitChange(limit=30_000, note="آخر ماه"), actor="owner")
            for bad in (CreditLimitChange(limit=-1), CreditLimitChange(limit=True),  # type: ignore[arg-type]
                        CreditLimitChange(limit=1, temp_limit=5, temp_limit_until=NOW - timedelta(days=1))):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    await set_credit_limit(session, 5, bad, actor="owner")
            events = (await session.scalars(select(CreditEvent).where(CreditEvent.shop_id == 5))).all()
            other = await session.scalar(select(ShopCreditAccount).where(ShopCreditAccount.shop_id == 6))
        self.assertEqual([(e.kind, e.old_value, e.new_value) for e in events],
                         [("limit", "0", "20000"), ("limit", "20000", "30000")])
        self.assertEqual(events[-1].note, "آخر ماه")
        self.assertIsNone(other)

    async def test_use_alerts_are_sent_once_per_level_and_rearm(self) -> None:
        async with self.Session() as session:
            levels = [await claim_credit_alert(session, 5, balance=b, limit=20_000)
                      for b in (-10_000, -14_000, -14_500, -18_000, -20_000, -20_000)]
            self.assertEqual(levels, [None, 70, None, 90, 100, None])
            self.assertIsNone(await claim_credit_alert(session, 5, balance=1_000, limit=20_000))
            self.assertEqual(await claim_credit_alert(session, 5, balance=-15_000, limit=20_000), 70)

    async def test_a_charge_sends_the_alert(self) -> None:
        from app.services.payg_gate import charge_shop_change

        with patch("app.services.payg_gate.notify_credit_use", AsyncMock()) as notify:
            async with self.Session() as session:
                await charge_shop_change(session, shop_id=5, change=request(20, "x").change,
                                         request_key="order:alert", pg_username="c")
        notify.assert_awaited_once()
        self.assertEqual(notify.await_args.kwargs["level"], 70)


class TemplateTests(unittest.TestCase):
    def test_owner_card_escapes_notes(self) -> None:
        from jinja2 import ChainableUndefined

        from app.api.app import templates
        from app.services.shop_credit import CreditOverview, credit_usage

        event = SimpleNamespace(created_at=None, kind="limit", old_value="0", new_value="1", actor="o",
                                note="<img src=x onerror=1>")
        credit = CreditOverview(account=SimpleNamespace(credit_limit=1, temp_limit=None, status="active"),
                                usage=credit_usage(-1, 1), balance=-1, events=(event,))
        env = templates.env.overlay(undefined=ChainableUndefined)
        html = env.get_template("_reseller_edit_body.html").render(
            request=SimpleNamespace(query_params={}, url=SimpleNamespace(path="/x"), cookies={}),
            staff={"role": "admin"}, user=SimpleNamespace(id=5), profile=SimpleNamespace(), is_payg=True,
            credit=credit, feature_perms=[], selected_perms=set(), pg_roles=[],
        )
        self.assertIn('action="/resellers/5/credit"', html)
        self.assertNotIn("<img src=x", html)


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade(self) -> None:
        path = ROOT / "alembic/versions/0099_shop_credit.py"
        spec = importlib.util.spec_from_file_location("shop_credit_migration", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        engine = sa.create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
            for step in (module.upgrade, module.upgrade, module.downgrade):
                with Operations.context(MigrationContext.configure(conn)):
                    step()
            self.assertFalse(sa.inspect(conn).has_table("shop_credit_accounts"))
            self.assertFalse(sa.inspect(conn).has_table("credit_events"))
            self.assertTrue(sa.inspect(conn).has_table("bot_users"))


if __name__ == "__main__":
    unittest.main()
