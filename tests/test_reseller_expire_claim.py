"""Atomic expiry claim: a concurrent renew wins; a due row expires once."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, PgAdminSubscription, ResellerPlan, ResellerProfile
from app.services.billing import GB
from app.services.pg_admin_subscription import (
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    expire_subscription,
    renew_subscription,
    run_subscription_expiry_tick,
)


class ExpireClaimTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        past = datetime.now(timezone.utc) - timedelta(days=1)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=3, telegram_id=33, referral_code="s3", role="reseller", wallet_balance=1_000_000),
                ResellerPlan(
                    id=1, name="fixed", plan_kind="subscription", billing_mode="fixed",
                    duration_days=30, included_users=10, included_gb=100, price=100_000,
                    is_active=True, create_pg_admin=True, pg_role_id=2,
                ),
                ResellerProfile(id=1, user_id=3, pg_admin_username="shop3", plan_id=1, is_active=True),
                PgAdminSubscription(
                    id=1, pg_username="shop3", plan_id=1, access_status=STATUS_ACTIVE,
                    base_users=10, base_gb=100, renew_generation=0, expires_at=past,
                ),
            ])
            await session.commit()
        self.pg = SimpleNamespace(
            get_admin=AsyncMock(return_value={"data_limit": 100 * GB, "max_users": 10}),
            modify_admin=AsyncMock(return_value={}),
            get_users=AsyncMock(return_value={"users": [{"id": 9}]}),
            set_disabled_by_id=AsyncMock(return_value={}),
            reset_admin_usage=AsyncMock(return_value={}),
        )
        self.patches = [
            patch("app.services.pasarguard.get_pg", return_value=self.pg),
            patch("app.services.billing_suspend._set_admin_enabled", AsyncMock()),
            patch("app.services.pg_admin_subscription.assert_child_growth_fits", AsyncMock()),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        await self.engine.dispose()

    async def test_due_subscription_is_claimed_once(self) -> None:
        async with self.Session() as session:
            sub = await session.get(PgAdminSubscription, 1)
            first = await expire_subscription(session, sub, commit=True)
            self.assertEqual(first.get("skipped"), 0)
            self.assertEqual(sub.access_status, STATUS_EXPIRED)
            second = await expire_subscription(session, sub, commit=True)
            self.assertEqual(second.get("already"), 1)

    async def test_renew_before_expire_claim_skips_expiry(self) -> None:
        async with self.Session() as session:
            sub = await session.get(PgAdminSubscription, 1)
            plan = await session.get(ResellerPlan, 1)
            owner = await session.get(BotUser, 3)
            await renew_subscription(
                session, sub=sub, plan=plan, payer=owner, charge_wallet=True, moving=False,
            )
            sub = await session.get(PgAdminSubscription, 1)
            self.assertEqual(sub.access_status, STATUS_ACTIVE)
            expires = sub.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            self.assertGreater(expires, datetime.now(timezone.utc))
            # Expire must not re-claim after renew moved expires_at into the future.
            outcome = await expire_subscription(session, sub, commit=True)
            self.assertEqual(outcome.get("skipped"), 1)
            sub = await session.get(PgAdminSubscription, 1)
            self.assertEqual(sub.access_status, STATUS_ACTIVE)

    async def test_tick_does_not_count_skipped_as_expired(self) -> None:
        async with self.Session() as session:
            sub = await session.get(PgAdminSubscription, 1)
            plan = await session.get(ResellerPlan, 1)
            owner = await session.get(BotUser, 3)
            await renew_subscription(
                session, sub=sub, plan=plan, payer=owner, charge_wallet=True, moving=False,
            )
            stats = await run_subscription_expiry_tick(session)
            self.assertEqual(stats["expired"], 0)
            sub = await session.get(PgAdminSubscription, 1)
            self.assertEqual(sub.access_status, STATUS_ACTIVE)


if __name__ == "__main__":
    unittest.main()
