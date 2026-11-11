"""PAYG: automatic billing enablement and tiered low-balance warnings."""

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
from app.db.models import BotUser, ResellerProfile, Role
from app.services import billing_alerts
from app.services.billing import (
    is_billing_enabled,
    maybe_warn_low_balance,
)
from app.services.billing_alerts import (
    claim_low_balance_warning,
    notify_low_balance,
    warn_band,
)
from app.services.org_principals import (
    DEPTH_TWO,
    bind_reseller_profile_principal,
    create_principal,
    ensure_owner_principal,
)
from app.services.resellers import make_reseller

PLATFORM_ADMIN_ID = 9001
MIGRATION_PATH = Path(__file__).resolve().parents[1] / "alembic/versions/0048_payg_low_balance_level.py"


class WarnBandTests(unittest.TestCase):
    def test_band_is_deepest_level_reached(self) -> None:
        self.assertIsNone(warn_band(100_001))
        self.assertEqual(warn_band(100_000), 100_000)
        self.assertEqual(warn_band(75_000), 100_000)
        self.assertEqual(warn_band(50_000), 50_000)
        self.assertEqual(warn_band(0), 50_000)
        self.assertEqual(warn_band(-5), 50_000)


class PaygAlertsDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _user(self, session: AsyncSession, telegram_id: int) -> BotUser:
        user = BotUser(
            telegram_id=telegram_id,
            role=Role.USER.value,
            referral_code=f"rc{telegram_id}",
        )
        session.add(user)
        await session.flush()
        return user

    async def _payg_profile(
        self, session: AsyncSession, telegram_id: int, *, username: str
    ) -> ResellerProfile:
        user = await self._user(session, telegram_id)
        return await make_reseller(
            session,
            user,
            web_username=username,
            web_password_hash="x" * 24,
            billing_mode="payg",
        )

    async def test_buying_a_payg_plan_turns_billing_on(self) -> None:
        async with self.Session() as session:
            self.assertFalse(await is_billing_enabled(session))
            await self._payg_profile(session, 7001, username="payg_one")
            self.assertTrue(await is_billing_enabled(session))

    async def test_fixed_plan_leaves_billing_off(self) -> None:
        async with self.Session() as session:
            user = await self._user(session, 7002)
            await make_reseller(
                session,
                user,
                web_username="fixed_one",
                web_password_hash="x" * 24,
                billing_mode="fixed",
            )
            self.assertFalse(await is_billing_enabled(session))

    async def test_each_level_warns_once(self) -> None:
        async with self.Session() as session:
            profile = await self._payg_profile(session, 7003, username="lvl")
            self.assertIsNone(await claim_low_balance_warning(session, profile, 150_000))
            self.assertEqual(await claim_low_balance_warning(session, profile, 90_000), 100_000)
            self.assertIsNone(await claim_low_balance_warning(session, profile, 80_000))
            self.assertEqual(await claim_low_balance_warning(session, profile, 40_000), 50_000)
            self.assertIsNone(await claim_low_balance_warning(session, profile, 30_000))

    async def test_topup_rearms_warnings(self) -> None:
        async with self.Session() as session:
            profile = await self._payg_profile(session, 7004, username="topup")
            await claim_low_balance_warning(session, profile, 40_000)
            self.assertIsNone(await claim_low_balance_warning(session, profile, 70_000))
            self.assertEqual(await claim_low_balance_warning(session, profile, 40_000), 50_000)
            self.assertIsNone(await claim_low_balance_warning(session, profile, 500_000))
            self.assertEqual(await claim_low_balance_warning(session, profile, 90_000), 100_000)

    async def test_concurrent_ticks_claim_a_level_only_once(self) -> None:
        async with self.Session() as setup:
            profile = await self._payg_profile(setup, 7005, username="race")
        async with self.Session() as first, self.Session() as second:
            stale_a = await first.get(ResellerProfile, profile.id)
            stale_b = await second.get(ResellerProfile, profile.id)
            won_a = await claim_low_balance_warning(first, stale_a, 40_000)
            won_b = await claim_low_balance_warning(second, stale_b, 40_000)
        self.assertEqual([won_a, won_b].count(50_000), 1)

    async def test_maybe_warn_ignores_fixed_resellers(self) -> None:
        async with self.Session() as session:
            user = await self._user(session, 7006)
            profile = await make_reseller(
                session,
                user,
                web_username="fixed_two",
                web_password_hash="x" * 24,
                billing_mode="fixed",
            )
            self.assertIsNone(await maybe_warn_low_balance(session, profile))

    async def test_maybe_warn_returns_level_and_balance(self) -> None:
        async with self.Session() as session:
            profile = await self._payg_profile(session, 7007, username="warn")
            with patch(
                "app.services.billing.payg_available_balance", AsyncMock(return_value=60_000)
            ):
                self.assertEqual(await maybe_warn_low_balance(session, profile), (100_000, 60_000))
                self.assertIsNone(await maybe_warn_low_balance(session, profile))

    async def test_platform_reseller_warns_reseller_and_platform_admins(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            profile = await self._payg_profile(session, 7008, username="plat")
            sent = await self._capture_notifications(session, profile)
        self.assertEqual(sent[0][1:3], ([7008], "reseller"))
        self.assertEqual(sent[1][1], [PLATFORM_ADMIN_ID])
        self.assertIn("plat", sent[1][2])
        self.assertEqual(self.markups, ["nv:res:ra:res_billing", f"adm:resellers:view:{profile.user_id}"])

    async def test_sub_reseller_warning_goes_to_its_parent_not_to_siblings(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            parent = await self._payg_profile(session, 7010, username="parent")
            sibling = await self._payg_profile(session, 7011, username="sibling")
            child_user = await self._user(session, 7012)
            child = await make_reseller(
                session,
                child_user,
                web_username="child",
                web_password_hash="x" * 24,
                billing_mode="payg",
            )
            parent_principal = await bind_reseller_profile_principal(session, parent)
            await bind_reseller_profile_principal(session, sibling)
            existing = await bind_reseller_profile_principal(session, child)
            await session.delete(existing)
            await session.flush()
            await create_principal(
                session,
                parent_id=parent_principal.id,
                depth=DEPTH_TWO,
                reseller_profile_id=child.id,
                bot_user_id=child.user_id,
            )
            await session.commit()
            sent = await self._capture_notifications(session, child)
        self.assertEqual(sent[1][0], parent.user_id)
        self.assertEqual(sent[1][1], [7010])
        self.assertNotIn(7011, sent[1][1])
        self.assertNotIn(PLATFORM_ADMIN_ID, sent[1][1])
        self.assertEqual(self.markups[1], None)

    async def _capture_notifications(
        self, session: AsyncSession, profile: ResellerProfile
    ) -> list[tuple[int | None, list[int], str]]:
        sent: list[tuple[int | None, list[int], str]] = []

        self.markups = []

        async def fake_send(_session, shop_user_id, chat_ids, text, *, markup=None) -> None:
            self.markups.append(None if markup is None else markup.inline_keyboard[0][0].callback_data)
            kind = "reseller" if shop_user_id == profile.user_id else "owner"
            sent.append((shop_user_id, list(chat_ids), text if kind == "owner" else "reseller"))

        settings = SimpleNamespace(admin_ids=[PLATFORM_ADMIN_ID])
        with (
            patch.object(billing_alerts, "_send", fake_send),
            patch.object(billing_alerts, "get_settings", return_value=settings),
        ):
            await notify_low_balance(session, profile, 50_000, 40_000)
        return sent


class LowBalanceLevelMigrationTests(unittest.TestCase):
    def test_upgrade_is_repeatable_and_downgrade_keeps_rows(self) -> None:
        spec = importlib.util.spec_from_file_location("low_balance_level_migration", MIGRATION_PATH)
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
                    sa.text("SELECT user_id, billing_low_warned_level FROM reseller_profiles")
                ).one()
                self.assertEqual(tuple(row), (42, None))
                migration.downgrade()
                self.assertEqual(
                    conn.execute(sa.text("SELECT user_id FROM reseller_profiles")).scalar(), 42
                )
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
