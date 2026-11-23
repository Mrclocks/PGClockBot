"""A reseller has one billing mode; switching it is confirmed and resets mode-specific state."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.db.models  # noqa: F401
from app.db import Base
from app.db.models import BotUser, ResellerPlan, ResellerProfile, Role
from app.services.billing import GB, PaygRateError, is_billing_enabled, run_billing_tick
from app.services.billing_mode import BillingModeChangeError, switch_billing_mode
from app.services.resellers import make_reseller, provision_existing_pg_admin
from tests.postgres_support import scoped_async_engine

PG_ADMIN = "shopadm"
RATE = 1000
WALLET = 1_000_000


class FakePanel:
    """PasarGuard stand-in: one admin with a settable traffic counter."""

    def __init__(self, used_bytes: int | None = 0, gate: str = "ok") -> None:
        self.used_bytes = used_bytes
        self.gate = gate
        self.admin_updates: list[dict] = []
        self.enabled_users: list[int] = []

    async def get_admin_gate(self, username: str) -> tuple[str, dict | None]:
        if self.gate != "ok":
            return self.gate, None
        return "ok", {"username": username, "lifetime_used_traffic": self.used_bytes}

    async def modify_admin(self, username: str, payload: dict) -> None:
        self.admin_updates.append(payload)

    async def set_disabled_by_id(self, user_id: int, disabled: bool) -> None:
        if not disabled:
            self.enabled_users.append(user_id)


class BillingModeTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        self.panel = FakePanel()
        patcher = patch("app.services.pasarguard.get_pg", side_effect=lambda: self.panel)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _plan(self, session: AsyncSession, *, mode: str, rate: int = RATE) -> ResellerPlan:
        plan = ResellerPlan(
            name=f"{mode}-{rate}",
            billing_mode=mode,
            price_per_gb=rate if mode == "payg" else 0,
            is_active=True,
        )
        session.add(plan)
        await session.flush()
        return plan

    async def _reseller(
        self,
        session: AsyncSession,
        telegram_id: int,
        *,
        mode: str,
        rate: int = RATE,
        watermark: int | None = None,
    ) -> ResellerProfile:
        plan = await self._plan(session, mode=mode, rate=rate)
        user = BotUser(
            telegram_id=telegram_id,
            role=Role.USER.value,
            referral_code=f"bm{telegram_id}",
            wallet_balance=WALLET,
        )
        session.add(user)
        await session.flush()
        return await make_reseller(
            session,
            user,
            web_username=f"shop{telegram_id}",
            web_password_hash="x" * 24,
            pg_admin_username=PG_ADMIN,
            plan_id=plan.id,
            billing_mode=mode,
            watermark_bytes=watermark,
        )

    async def _reload(self, profile: ResellerProfile) -> ResellerProfile:
        async with self.Session() as fresh:
            return (
                await fresh.execute(
                    select(ResellerProfile).where(ResellerProfile.id == profile.id)
                )
            ).scalar_one()


class SwitchBillingModeTests(BillingModeTestCase):
    async def test_changing_mode_requires_confirmation(self) -> None:
        async with self.Session() as session:
            profile = await self._reseller(session, 8201, mode="fixed")
            plan = await self._plan(session, mode="payg")
            profile.plan_id = plan.id
            with self.assertRaises(BillingModeChangeError):
                await switch_billing_mode(session, profile, "payg", confirmed=False)
        self.assertEqual((await self._reload(profile)).billing_mode, "fixed")

    async def test_confirmed_switch_to_payg_bills_only_usage_after_the_switch(self) -> None:
        self.panel.used_bytes = 5 * GB
        async with self.Session() as session:
            profile = await self._reseller(session, 8202, mode="fixed")
            profile.plan_id = (await self._plan(session, mode="payg")).id
            self.assertFalse(await is_billing_enabled(session))
            await switch_billing_mode(session, profile, "payg", confirmed=True)
            self.assertTrue(await is_billing_enabled(session))
            self.assertEqual(profile.billing_watermark_bytes, 5 * GB)

            self.panel.used_bytes = 6 * GB
            await run_billing_tick(session)
            wallet = (await session.get(BotUser, profile.user_id)).wallet_balance
        self.assertEqual(wallet, WALLET - RATE)

    async def test_switch_to_payg_is_refused_while_panel_is_unreachable(self) -> None:
        self.panel.gate = "unreachable"
        async with self.Session() as session:
            profile = await self._reseller(session, 8203, mode="fixed")
            profile.plan_id = (await self._plan(session, mode="payg")).id
            with self.assertRaises(BillingModeChangeError):
                await switch_billing_mode(session, profile, "payg", confirmed=True)
        self.assertEqual((await self._reload(profile)).billing_mode, "fixed")

    async def test_switch_to_payg_is_refused_without_a_positive_rate(self) -> None:
        async with self.Session() as session:
            profile = await self._reseller(session, 8204, mode="fixed")
            with self.assertRaises(PaygRateError):
                await switch_billing_mode(session, profile, "payg", confirmed=True)
        self.assertEqual((await self._reload(profile)).billing_mode, "fixed")

    async def test_back_to_fixed_lifts_suspension_and_clears_payg_state(self) -> None:
        async with self.Session() as session:
            profile = await self._reseller(session, 8205, mode="payg")
            profile.billing_suspended_at = datetime.now(timezone.utc)
            profile.billing_suspended_user_ids = "[41,42]"
            profile.billing_low_warned_level = 50_000
            profile.billing_tick_issue = "rate_zero"
            profile.plan_id = (await self._plan(session, mode="fixed")).id
            await session.commit()

            await switch_billing_mode(session, profile, "fixed", confirmed=True)
        stored = await self._reload(profile)
        self.assertEqual(stored.billing_mode, "fixed")
        self.assertIsNone(stored.billing_suspended_at)
        self.assertIsNone(stored.billing_low_warned_level)
        self.assertIsNone(stored.billing_tick_issue)
        self.assertEqual(self.panel.enabled_users, [41, 42])

    async def test_same_mode_is_a_no_op(self) -> None:
        async with self.Session() as session:
            profile = await self._reseller(session, 8206, mode="fixed")
            change = await switch_billing_mode(session, profile, "fixed", confirmed=False)
        self.assertFalse(change.changed)


class MakeResellerModeTests(BillingModeTestCase):
    async def test_active_reseller_cannot_be_moved_to_another_mode_silently(self) -> None:
        async with self.Session() as session:
            profile = await self._reseller(session, 8301, mode="fixed")
            user = await session.get(BotUser, profile.user_id)
            with self.assertRaises(BillingModeChangeError):
                await make_reseller(session, user, billing_mode="payg")

    async def test_confirmed_regrant_applies_mode_and_starting_watermark(self) -> None:
        async with self.Session() as session:
            profile = await self._reseller(session, 8302, mode="fixed")
            user = await session.get(BotUser, profile.user_id)
            await make_reseller(
                session,
                user,
                billing_mode="payg",
                confirm_mode_change=True,
                watermark_bytes=7 * GB,
            )
        stored = await self._reload(profile)
        self.assertEqual((stored.billing_mode, stored.billing_watermark_bytes), ("payg", 7 * GB))

    async def test_revoked_reseller_can_be_granted_a_different_mode(self) -> None:
        async with self.Session() as session:
            profile = await self._reseller(session, 8303, mode="fixed")
            profile.is_active = False
            user = await session.get(BotUser, profile.user_id)
            await make_reseller(session, user, billing_mode="payg", watermark_bytes=0)
        self.assertEqual((await self._reload(profile)).billing_mode, "payg")

    async def test_new_payg_reseller_on_existing_admin_starts_at_current_traffic(self) -> None:
        async with self.Session() as session:
            user = BotUser(telegram_id=8304, role=Role.USER.value, referral_code="bm8304")
            session.add(user)
            await session.flush()
            profile = await make_reseller(
                session, user, billing_mode="payg", watermark_bytes=3 * GB
            )
        self.assertEqual((await self._reload(profile)).billing_watermark_bytes, 3 * GB)


class RegrantExistingAdminTests(BillingModeTestCase):
    async def _grant(self, session: AsyncSession, plan: ResellerPlan, *, confirm: bool):
        with patch(
            "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
            AsyncMock(return_value=None),
        ):
            return await provision_existing_pg_admin(
                session,
                pg_username=PG_ADMIN,
                web_username=PG_ADMIN,
                password="Str0ng-Passw0rd!",
                plan_id=plan.id,
                confirm_mode_change=confirm,
            )

    async def test_granting_a_plan_of_the_other_mode_needs_confirmation(self) -> None:
        async with self.Session() as session:
            profile = await self._reseller(session, 8401, mode="fixed")
            payg_plan = await self._plan(session, mode="payg")
            granted, _hint, error = await self._grant(session, payg_plan, confirm=False)
        self.assertIsNone(granted)
        self.assertIn("تأیید تغییر نوع پرداخت", error or "")
        self.assertEqual((await self._reload(profile)).billing_mode, "fixed")

    async def test_confirmed_grant_starts_payg_at_current_admin_traffic(self) -> None:
        self.panel.used_bytes = 9 * GB
        async with self.Session() as session:
            profile = await self._reseller(session, 8402, mode="fixed")
            payg_plan = await self._plan(session, mode="payg")
            with patch("app.services.pg_admin_subscription.start_or_refresh_subscription", AsyncMock()):
                granted, _hint, error = await self._grant(session, payg_plan, confirm=True)
        self.assertIsNone(error)
        self.assertIsNotNone(granted)
        stored = await self._reload(profile)
        self.assertEqual((stored.billing_mode, stored.billing_watermark_bytes), ("payg", 9 * GB))


if __name__ == "__main__":
    unittest.main()
