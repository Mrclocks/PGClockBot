"""Reconciliation charges PAYG shops for panel changes made outside the bot, exactly once."""

from __future__ import annotations

import importlib.util
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

from app.db.models import (
    Base,
    BotUser,
    PaygPanelSnapshot,
    ResellerCharge,
    ResellerPlan,
    ResellerProfile,
    UserService,
)
from app.services.payg_pricing import GB, ChargeEvent
from app.services.payg_reconcile import Paid, panel_user, run_payg_reconcile, uncovered_changes

ROOT = Path(__file__).resolve().parents[1]
SHOP = 5
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def user(uid: int, gb: int, *, used: int = 0, expire: int | None = None, name: str | None = None) -> dict:
    return {"id": uid, "username": name or f"u{uid}", "data_limit": gb * GB, "used_traffic": used, "expire": expire}


class FakePanel:
    def __init__(self, users: list[dict]) -> None:
        self.users = users

    async def get_users(self, *, admin: str, limit: int, offset: int, cache_max_age: float) -> dict:
        return {"users": self.users[offset:offset + limit]}


class PureTests(unittest.TestCase):
    def snap(self, gb: int, *, used: int = 0, expire: int | None = None) -> SimpleNamespace:
        return SimpleNamespace(data_limit=gb * GB, used_traffic=used, expire_ts=expire, seen_at=T0, revision=0)

    def live(self, gb: int, *, used: int = 0, expire: int | None = None):
        return panel_user(user(1, gb, used=used, expire=expire))

    def test_uncovered_volume_increase_is_charged(self) -> None:
        [change] = uncovered_changes(self.snap(10), self.live(25), Paid())
        self.assertEqual((change.event, change.data_limit_bytes), (ChargeEvent.ADD_VOLUME, 15 * GB))

    def test_bot_paid_increase_is_not_charged_again(self) -> None:
        self.assertEqual(uncovered_changes(self.snap(10), self.live(25), Paid(bytes=15 * GB)), [])
        [rest] = uncovered_changes(self.snap(10), self.live(25), Paid(bytes=5 * GB))
        self.assertEqual(rest.data_limit_bytes, 10 * GB)

    def test_reset_in_the_panel_charges_the_whole_volume(self) -> None:
        [change] = uncovered_changes(self.snap(30, used=20 * GB), self.live(30, used=0), Paid())
        self.assertEqual((change.event, change.data_limit_bytes), (ChargeEvent.RESET, 30 * GB))

    def test_a_renewal_covers_only_what_it_paid_for(self) -> None:
        renewed = self.live(50, used=0)
        self.assertEqual(uncovered_changes(self.snap(10, used=GB), renewed, Paid(renewed=True, bytes=50 * GB)), [])
        # Renewed for 30 GB, then 20 GB more added directly in the panel in the same window.
        [extra] = uncovered_changes(self.snap(10, used=GB), renewed, Paid(renewed=True, bytes=30 * GB))
        self.assertEqual((extra.event, extra.data_limit_bytes), (ChargeEvent.ADD_VOLUME, 20 * GB))

    def test_extra_days_and_unlimited(self) -> None:
        start = int(T0.timestamp())
        changes = uncovered_changes(self.snap(10, expire=start + 5 * 86400), self.live(10, expire=start + 35 * 86400), Paid())
        self.assertEqual([(c.event, c.days) for c in changes], [(ChargeEvent.ADD_DAYS, 30)])
        [unlimited] = uncovered_changes(self.snap(10), self.live(0), Paid())
        self.assertEqual((unlimited.event, unlimited.data_limit_bytes), (ChargeEvent.ADD_VOLUME, 0))

    def test_malformed_panel_users_are_skipped(self) -> None:
        for raw in (None, {}, {"id": True, "username": "x"}, {"id": 3, "username": "", "data_limit": 1},
                    {"id": 3, "username": "x", "data_limit": -1}, {"id": 3, "username": "x", "used_traffic": "a"}):
            with self.subTest(raw=raw):
                self.assertIsNone(panel_user(raw))


class ReconcileCase(unittest.IsolatedAsyncioTestCase):
    upfront = True

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP, telegram_id=5, referral_code="shop", role="reseller", wallet_balance=100_000),
                BotUser(id=9, telegram_id=9, referral_code="buyer"),
                ResellerPlan(id=1, name="payg", billing_mode="payg", price_per_gb=1000,
                             payg_basis="traffic", payg_charge_upfront=self.upfront),
            ])
            session.add(ResellerProfile(id=1, user_id=SHOP, plan_id=1, billing_mode="payg", is_active=True,
                                        payg_wallet_linked=True, pg_admin_username="shop_a"))
            await session.commit()
        self.alerts = patch("app.services.billing_alerts._send", AsyncMock())
        self.alerts.start()
        self.addCleanup(self.alerts.stop)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def run_once(self, users: list[dict], at: datetime):
        async with self.Session() as session:
            return await run_payg_reconcile(session, FakePanel(users), now=at)

    async def wallet(self) -> int:
        async with self.Session() as session:
            return int((await session.get(BotUser, SHOP)).wallet_balance)

    async def count(self, model) -> int:
        async with self.Session() as session:
            return int(await session.scalar(select(func.count()).select_from(model)))


class ReconcileTests(ReconcileCase):
    async def test_first_run_is_a_baseline_and_later_panel_changes_are_charged_once(self) -> None:
        stats = await self.run_once([user(1, 10)], T0)
        self.assertEqual((stats.baseline, stats.charged), (1, 0))
        self.assertEqual(await self.wallet(), 100_000)
        later = [user(1, 30), user(2, 20)]
        await self.run_once(later, T0 + timedelta(minutes=10))
        self.assertEqual(await self.wallet(), 100_000 - 20_000 - 20_000)
        # Nothing new happened: a rerun charges nothing.
        await self.run_once(later, T0 + timedelta(minutes=20))
        self.assertEqual(await self.wallet(), 60_000)

    async def test_panel_charges_are_settled_and_never_swept_to_review(self) -> None:
        await self.run_once([user(1, 10)], T0)
        await self.run_once([user(1, 30), user(2, 20)], T0 + timedelta(minutes=10))
        await self.run_once([user(1, 30), user(2, 20)], T0 + timedelta(hours=4))  # past the pending sweep
        async with self.Session() as session:
            statuses = sorted((await session.scalars(select(ResellerCharge.status))).all())
        self.assertEqual(statuses, ["settled", "settled"])

    async def test_a_shop_with_no_users_still_takes_its_baseline(self) -> None:
        stats = await self.run_once([], T0)
        self.assertEqual(stats.baseline, 0)
        await self.run_once([user(1, 10)], T0 + timedelta(minutes=10))
        self.assertEqual(await self.wallet(), 90_000)  # the first panel user is charged, not taken as baseline

    async def renewed_by_bot(self, gb: int) -> None:
        async with self.Session() as session:
            session.add(ResellerCharge(
                shop_id=SHOP, event="renew", status="settled", request_key="svc-renew:x", attempt=1,
                quantity=1, pg_username="u1", bytes_charged=gb * GB, days_charged=30, amount=gb * 1000,
                idempotency_key="payg:5:svc-renew:x#1", created_at=T0 + timedelta(minutes=5),
            ))
            await session.commit()

    async def test_a_bot_renewal_is_not_charged_again(self) -> None:
        await self.run_once([user(1, 10, used=8 * GB)], T0)
        await self.renewed_by_bot(30)
        await self.run_once([user(1, 30, used=0)], T0 + timedelta(minutes=10))
        self.assertEqual(await self.wallet(), 100_000)

    async def test_volume_added_in_the_panel_on_top_of_a_renewal_is_charged(self) -> None:
        await self.run_once([user(1, 10, used=8 * GB)], T0)
        await self.renewed_by_bot(30)
        await self.run_once([user(1, 50, used=0)], T0 + timedelta(minutes=10))
        self.assertEqual(await self.wallet(), 100_000 - 20_000)

    async def test_users_made_by_the_bot_are_not_charged_as_new(self) -> None:
        await self.run_once([user(1, 10)], T0)
        async with self.Session() as session:
            session.add(UserService(bot_user_id=9, pg_user_id=2, pg_username="u2", shop_id=SHOP))
            await session.commit()
        await self.run_once([user(1, 10), user(2, 50)], T0 + timedelta(minutes=10))
        self.assertEqual(await self.wallet(), 100_000)

    async def test_a_bot_charge_since_the_snapshot_is_subtracted(self) -> None:
        await self.run_once([user(1, 10)], T0)
        async with self.Session() as session:
            session.add(ResellerCharge(
                shop_id=SHOP, event="add_volume", status="settled", request_key="svc-edit:x", attempt=1,
                quantity=1, pg_username="u1", bytes_charged=15 * GB, amount=15_000,
                idempotency_key="payg:5:svc-edit:x#1", created_at=T0 + timedelta(minutes=5),
            ))
            await session.commit()
        await self.run_once([user(1, 25)], T0 + timedelta(minutes=10))
        self.assertEqual(await self.wallet(), 100_000)

    async def test_shortfall_keeps_the_difference_for_the_next_run(self) -> None:
        await self.run_once([user(1, 10)], T0)
        stats = await self.run_once([user(1, 210)], T0 + timedelta(minutes=10))
        self.assertEqual(stats.short, 1)
        self.assertEqual(await self.wallet(), 100_000)
        async with self.Session() as session:
            shop = await session.get(BotUser, SHOP)
            shop.wallet_balance = 300_000
            await session.commit()
        await self.run_once([user(1, 210)], T0 + timedelta(minutes=20))
        self.assertEqual(await self.wallet(), 100_000)

    async def test_stale_pending_charges_go_to_review(self) -> None:
        async with self.Session() as session:
            session.add(ResellerCharge(
                shop_id=SHOP, event="create", status="pending", request_key="order:1", attempt=1, quantity=1,
                amount=5_000, idempotency_key="payg:5:order:1#1", created_at=T0,
            ))
            await session.commit()
        stats = await self.run_once([], T0 + timedelta(hours=3))
        self.assertEqual(stats.swept, 1)
        async with self.Session() as session:
            self.assertEqual((await session.scalar(select(ResellerCharge))).status, "review")


class UsageBilledTests(ReconcileCase):
    upfront = False

    async def test_usage_billed_shop_is_skipped_and_keeps_no_baseline(self) -> None:
        async with self.Session() as session:
            session.add(PaygPanelSnapshot(shop_id=SHOP, pg_user_id=1, pg_username="u1", data_limit=GB, seen_at=T0))
            await session.commit()
        stats = await self.run_once([user(1, 99)], T0 + timedelta(minutes=10))
        self.assertEqual(stats.shops, 0)
        self.assertEqual(await self.wallet(), 100_000)
        self.assertEqual(await self.count(PaygPanelSnapshot), 0)


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade(self) -> None:
        path = ROOT / "alembic/versions/0098_payg_panel_snapshots.py"
        spec = importlib.util.spec_from_file_location("payg_snapshots_migration", path)
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
            self.assertTrue(sa.inspect(conn).has_table("payg_panel_snapshots"))
            with Operations.context(MigrationContext.configure(conn)):
                module.downgrade()
            self.assertFalse(sa.inspect(conn).has_table("payg_panel_snapshots"))
            self.assertTrue(sa.inspect(conn).has_table("bot_users"))


class BaselineMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_rows(self) -> None:
        path = ROOT / "alembic/versions/0103_payg_baseline_at.py"
        spec = importlib.util.spec_from_file_location("payg_baseline_migration", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        engine = sa.create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE reseller_profiles (id INTEGER PRIMARY KEY, user_id INTEGER)"))
            conn.execute(sa.text("INSERT INTO reseller_profiles (id, user_id) VALUES (1, 5)"))
            for step in (module.upgrade, module.upgrade):
                with Operations.context(MigrationContext.configure(conn)):
                    step()
            self.assertIn("payg_baseline_at", {c["name"] for c in sa.inspect(conn).get_columns("reseller_profiles")})
            with Operations.context(MigrationContext.configure(conn)):
                module.downgrade()
            self.assertNotIn("payg_baseline_at", {c["name"] for c in sa.inspect(conn).get_columns("reseller_profiles")})
            self.assertEqual(conn.execute(sa.text("SELECT user_id FROM reseller_profiles")).scalar(), 5)


if __name__ == "__main__":
    unittest.main()
