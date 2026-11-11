"""The billing tick tells the owning admin when a PAYG reseller cannot be billed."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401
from app.db import Base
from app.db.models import BotUser, ResellerPlan, ResellerProfile, Role
from app.services import billing_alerts
from app.services.billing import GB, run_billing_tick
from app.services.billing_alerts import (
    PANEL_TICK_ISSUES,
    TickIssue,
    clear_tick_issue,
    report_tick_issue,
)
from app.services.resellers import make_reseller

PLATFORM_ADMIN_ID = 9001
MIGRATION_PATH = Path(__file__).resolve().parents[1] / "alembic/versions/0049_payg_tick_issue.py"
RICH_WALLET = 1_000_000


class FakePanel:
    def __init__(self, admins: dict[str, tuple[str, dict | None]]) -> None:
        self.admins = admins

    async def get_admin_gate(self, username: str) -> tuple[str, dict | None]:
        return self.admins.get(username, ("missing", None))


class TickIssueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        self.sent: list[tuple[list[int], str]] = []
        self.markups: list[object] = []

        async def fake_send(_session, _shop_user_id, chat_ids, text, *, markup=None) -> None:
            self.sent.append((list(chat_ids), text))
            self.markups.append(markup)

        settings = SimpleNamespace(admin_ids=[PLATFORM_ADMIN_ID])
        patches = [
            patch.object(billing_alerts, "_send", fake_send),
            patch.object(billing_alerts, "get_settings", return_value=settings),
        ]
        for active in patches:
            active.start()
            self.addCleanup(active.stop)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _payg_reseller(
        self,
        session: AsyncSession,
        telegram_id: int,
        *,
        pg_username: str | None,
        plan_rate: int = 1000,
    ) -> ResellerProfile:
        plan = ResellerPlan(
            name=f"plan{telegram_id}", billing_mode="payg", price_per_gb=plan_rate, is_active=True
        )
        session.add(plan)
        user = BotUser(
            telegram_id=telegram_id,
            role=Role.USER.value,
            referral_code=f"ti{telegram_id}",
            wallet_balance=RICH_WALLET,
        )
        session.add(user)
        await session.flush()
        return await make_reseller(
            session,
            user,
            web_username=f"shop{telegram_id}",
            web_password_hash="x" * 24,
            pg_admin_username=pg_username,
            plan_id=plan.id,
            billing_mode="payg",
        )

    async def _tick(self, session: AsyncSession, panel: FakePanel) -> dict[str, int]:
        with patch("app.services.pasarguard.get_pg", return_value=panel):
            return await run_billing_tick(session)

    async def test_issue_is_announced_once_until_it_changes_or_clears(self) -> None:
        async with self.Session() as session:
            profile = await self._payg_reseller(session, 8101, pg_username=None)
            self.assertTrue(await report_tick_issue(session, profile, TickIssue.NO_ADMIN))
            self.assertFalse(await report_tick_issue(session, profile, TickIssue.NO_ADMIN))
            self.assertTrue(await report_tick_issue(session, profile, TickIssue.RATE_ZERO))
            await clear_tick_issue(session, profile, (TickIssue.RATE_ZERO,))
            self.assertTrue(await report_tick_issue(session, profile, TickIssue.RATE_ZERO))
        self.assertEqual(len(self.sent), 3)
        self.assertTrue(all(chat_ids == [PLATFORM_ADMIN_ID] for chat_ids, _ in self.sent))
        self.assertEqual(
            [b.callback_data for b in self.markups[0].inline_keyboard[0]],
            [f"adm:resellers:view:{profile.user_id}"],
        )

    async def test_tick_without_pg_admin_tells_owner_once(self) -> None:
        async with self.Session() as session:
            await self._payg_reseller(session, 8102, pg_username=None)
            first = await self._tick(session, FakePanel({}))
            second = await self._tick(session, FakePanel({}))
        self.assertEqual((first["skipped"], second["skipped"]), (1, 1))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("shop8102", self.sent[0][1])

    async def test_unreachable_panel_is_reported_and_recovery_rearms(self) -> None:
        async with self.Session() as session:
            profile = await self._payg_reseller(session, 8103, pg_username="adm8103")
            await self._payg_reseller(session, 8106, pg_username="adm8106")
            healthy_peer = ("ok", {"lifetime_used_traffic": 0})
            await self._tick(
                session, FakePanel({"adm8103": ("unreachable", None), "adm8106": healthy_peer})
            )
            self.assertEqual(profile.billing_tick_issue, TickIssue.PANEL_UNAVAILABLE.value)
            healthy = FakePanel({"adm8103": ("ok", {"lifetime_used_traffic": 0}), "adm8106": healthy_peer})
            await self._tick(session, healthy)
            self.assertIsNone(profile.billing_tick_issue)
            await self._tick(
                session, FakePanel({"adm8103": ("unreachable", None), "adm8106": healthy_peer})
            )
        self.assertEqual(len(self.sent), 2)

    async def test_global_panel_outage_does_not_alert_resellers(self) -> None:
        async with self.Session() as session:
            first = await self._payg_reseller(session, 8107, pg_username="adm8107")
            second = await self._payg_reseller(session, 8108, pg_username="adm8108")
            down = ("unreachable", None)
            await self._tick(session, FakePanel({"adm8107": down, "adm8108": down}))
            self.assertIsNone(first.billing_tick_issue)
            self.assertIsNone(second.billing_tick_issue)
        self.assertEqual(self.sent, [])

    async def test_missing_admin_and_missing_traffic_field_are_distinct_issues(self) -> None:
        async with self.Session() as session:
            gone = await self._payg_reseller(session, 8104, pg_username="gone")
            blind = await self._payg_reseller(session, 8105, pg_username="blind")
            await self._tick(
                session,
                FakePanel({"gone": ("missing", None), "blind": ("ok", {"username": "blind"})}),
            )
        self.assertEqual(gone.billing_tick_issue, TickIssue.ADMIN_MISSING.value)
        self.assertEqual(blind.billing_tick_issue, TickIssue.NO_TRAFFIC_FIELD.value)

    async def test_zero_rate_usage_is_held_and_reported(self) -> None:
        async with self.Session() as session:
            profile = await self._payg_reseller(
                session, 8106, pg_username="free", plan_rate=0
            )
            panel = FakePanel({"free": ("ok", {"lifetime_used_traffic": 3 * GB})})
            await self._tick(session, panel)
            self.assertEqual(profile.billing_tick_issue, TickIssue.RATE_ZERO.value)
            self.assertEqual(profile.billing_watermark_bytes, 0)
        self.assertEqual(len(self.sent), 1)

    async def test_panel_issues_set_excludes_rate_zero(self) -> None:
        self.assertNotIn(TickIssue.RATE_ZERO, PANEL_TICK_ISSUES)


class TickIssueMigrationTests(unittest.TestCase):
    def test_upgrade_is_repeatable_and_downgrade_keeps_rows(self) -> None:
        spec = importlib.util.spec_from_file_location("tick_issue_migration", MIGRATION_PATH)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE reseller_profiles (id INTEGER PRIMARY KEY, user_id INTEGER)"))
            conn.execute(sa.text("INSERT INTO reseller_profiles VALUES (1, 42)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                row = conn.execute(
                    sa.text("SELECT user_id, billing_tick_issue FROM reseller_profiles")
                ).one()
                self.assertEqual(tuple(row), (42, None))
                migration.downgrade()
                self.assertEqual(
                    conn.execute(sa.text("SELECT user_id FROM reseller_profiles")).scalar(), 42
                )
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
