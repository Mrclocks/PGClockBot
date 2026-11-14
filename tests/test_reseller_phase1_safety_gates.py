"""Phase 1 reseller safety: open subscription gates; renew allowed after expiry soft-lock."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, PgAdminSubscription, ResellerPlan, ResellerProfile
from app.services.billing import GB
from app.services.reseller_package import renew_package
from app.services.reseller_packs import PACK_EXPIRED, buy_addon_pack
from app.services.reseller_shop_gates import SHOP_INACTIVE, SUB_EXPIRED, SUB_NEEDS_ACTIVE


class Phase1SafetyGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=3, telegram_id=33, referral_code="s3", role="reseller", wallet_balance=1_000_000),
            ResellerPlan(
                id=1, name="fixed", plan_kind="subscription", billing_mode="fixed", duration_days=30,
                included_users=10, included_gb=100, price=100_000, allow_buy_extra=True,
                extra_gb_price=1_000, addon_gb=10, is_active=True, create_pg_admin=True, pg_role_id=2,
                web_permissions='["orders"]',
            ),
            ResellerPlan(
                id=11, name="vol", plan_kind="addon_volume", billing_mode="fixed", addon_gb=10,
                price=50_000, is_active=True,
            ),
            ResellerProfile(
                id=1, user_id=3, pg_admin_username="shop3", plan_id=1, is_active=True,
                web_permissions='["orders"]',
            ),
            PgAdminSubscription(
                pg_username="shop3", plan_id=1, access_status="active",
                base_users=10, base_gb=100, renew_generation=0,
            ),
        ])
        await self.session.commit()
        self.pg = SimpleNamespace(
            get_admin=AsyncMock(return_value={"data_limit": 100 * GB, "max_users": 10, "total_users": 0, "used_traffic": 0}),
            modify_admin=AsyncMock(return_value={}),
            get_users=AsyncMock(return_value={"users": []}),
            reset_admin_usage=AsyncMock(return_value={}),
        )
        self.patches = [
            patch("app.services.pasarguard.get_pg", return_value=self.pg),
            patch("app.services.billing_suspend._set_admin_enabled", AsyncMock()),
            patch("app.services.pg_admin_subscription.assert_child_growth_fits", AsyncMock()),
            patch("app.services.reseller_capacity.assert_child_growth_fits", AsyncMock()),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        await self.session.close()
        await self.engine.dispose()

    async def test_soft_deactivate_while_open_blocks_renew_and_pack(self) -> None:
        profile = await self.session.get(ResellerProfile, 1)
        profile.is_active = False
        await self.session.commit()
        owner = await self.session.get(BotUser, 3)
        plan = await self.session.get(ResellerPlan, 1)
        with self.assertRaises(ValueError) as renew_err:
            await renew_package(self.session, owner=owner, profile=profile, plan_id=1, generation=0)
        self.assertEqual(str(renew_err.exception), SHOP_INACTIVE)
        with self.assertRaises(ValueError) as pack_err:
            await buy_addon_pack(
                self.session, user=owner, profile=profile, plan=plan, pack_id=11, generation=0,
            )
        self.assertEqual(str(pack_err.exception), SHOP_INACTIVE)

    async def test_expiry_soft_lock_still_allows_renew(self) -> None:
        """expire_subscription flips is_active off; the owner must still be able to renew."""
        from sqlalchemy import select

        profile = await self.session.get(ResellerProfile, 1)
        profile.is_active = False
        sub = await self.session.scalar(select(PgAdminSubscription))
        sub.access_status = "expired"
        sub.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        await self.session.commit()
        owner = await self.session.get(BotUser, 3)
        result = await renew_package(self.session, owner=owner, profile=profile, plan_id=1, generation=0)
        self.assertEqual(result["amount"], 100_000)
        pack_err = None
        plan = await self.session.get(ResellerPlan, 1)
        try:
            await buy_addon_pack(
                self.session, user=owner, profile=profile, plan=plan, pack_id=11, generation=1,
            )
        except ValueError as exc:
            pack_err = str(exc)
        self.assertIn(pack_err, {PACK_EXPIRED, SUB_EXPIRED, SUB_NEEDS_ACTIVE, SHOP_INACTIVE})

    async def test_expired_clock_blocks_pack_like_unit_extras(self) -> None:
        from sqlalchemy import select

        sub = await self.session.scalar(select(PgAdminSubscription))
        sub.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        await self.session.commit()
        owner = await self.session.get(BotUser, 3)
        profile = await self.session.get(ResellerProfile, 1)
        plan = await self.session.get(ResellerPlan, 1)
        with self.assertRaises(ValueError) as caught:
            await buy_addon_pack(
                self.session, user=owner, profile=profile, plan=plan, pack_id=11, generation=0,
            )
        self.assertIn(str(caught.exception), {PACK_EXPIRED, SUB_EXPIRED, SUB_NEEDS_ACTIVE})


if __name__ == "__main__":
    unittest.main()
