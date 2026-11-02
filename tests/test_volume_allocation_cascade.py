"""Level-1 shops pass volume to level-2 children only within their own ceiling."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import (
    BotUser,
    OrgPrincipal,
    PgAdminSubscription,
    ResellerProfile,
    Role,
    UserService,
)
from app.services.billing import GB
from app.services.org_principals import DEPTH_ONE, DEPTH_TWO, STATUS_ACTIVE
from app.services.pg_admin_subscription import admin_adjust_reseller_subscription, record_unit_extra
from app.services.volume_allocation import (
    VolumeAllocationError,
    assert_volume_delta_allowed,
    sum_allocated_bytes,
)
from app.services.volume_allocation_cascade import (
    ParentVolumeError,
    assert_child_growth_fits,
    assert_parent_has_room_for_reenabled,
    list_uncapped_children,
)


class CascadeHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'casc.db'}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def _tree(
        self,
        *,
        parent_ceiling_gb: int = 100,
        child_ceiling_gb: int | None = 40,
    ) -> tuple[int, int, int, int]:
        """Return (parent_shop, child_shop, parent_principal_id, child_principal_id)."""
        async with self.Session() as session:
            session.add(
                BotUser(id=10, telegram_id=10, role=Role.RESELLER.value, referral_code="p")
            )
            session.add(
                BotUser(id=20, telegram_id=20, role=Role.RESELLER.value, referral_code="c")
            )
            parent_prof = ResellerProfile(
                user_id=10,
                pg_admin_username="parent-admin",
                billing_mode="fixed",
                allow_volume_oversell=False,
                is_active=True,
            )
            child_prof = ResellerProfile(
                user_id=20,
                pg_admin_username="child-admin",
                billing_mode="fixed",
                allow_volume_oversell=False,
                is_active=True,
            )
            session.add(parent_prof)
            session.add(child_prof)
            await session.flush()
            session.add(
                PgAdminSubscription(
                    pg_username="parent-admin",
                    base_gb=parent_ceiling_gb,
                    extra_gb_purchased=0,
                )
            )
            if child_ceiling_gb:
                session.add(
                    PgAdminSubscription(
                        pg_username="child-admin",
                        base_gb=child_ceiling_gb,
                        extra_gb_purchased=0,
                    )
                )
            parent = OrgPrincipal(
                parent_id=None,
                depth=DEPTH_ONE,
                status=STATUS_ACTIVE,
                pg_username="parent-admin",
                reseller_profile_id=int(parent_prof.id),
            )
            session.add(parent)
            await session.flush()
            child = OrgPrincipal(
                parent_id=int(parent.id),
                depth=DEPTH_TWO,
                status=STATUS_ACTIVE,
                pg_username="child-admin",
                reseller_profile_id=int(child_prof.id),
            )
            session.add(child)
            await session.flush()
            await session.commit()
            return 10, 20, int(parent.id), int(child.id)


class ParentBudgetTests(CascadeHarness):
    async def _adjust_child(self, gb: int) -> None:
        async with self.Session() as session:
            profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == 20))
            with patch("app.services.pg_admin_subscription.sync_pg_capacity_from_sub", new=AsyncMock()), patch(
                "app.services.pg_admin_subscription._assert_owner_capacity_budget", new=AsyncMock()
            ):
                await admin_adjust_reseller_subscription(session, profile, extra_gb=gb)

    async def _child_total(self) -> int:
        async with self.Session() as session:
            sub = await session.scalar(select(PgAdminSubscription).where(PgAdminSubscription.pg_username == "child-admin"))
            return int(sub.base_gb or 0) + int(sub.extra_gb_purchased or 0)

    async def test_child_ceiling_counts_against_the_parent(self) -> None:
        await self._tree(parent_ceiling_gb=100, child_ceiling_gb=40)
        async with self.Session() as session:
            self.assertEqual(await sum_allocated_bytes(session, 10), 40 * GB)
            await assert_volume_delta_allowed(session, shop_id=10, delta_bytes=60 * GB)
            with self.assertRaises(VolumeAllocationError):
                await assert_volume_delta_allowed(session, shop_id=10, delta_bytes=61 * GB)

    async def test_raise_beyond_parent_room_is_refused_and_not_saved(self) -> None:
        await self._tree(parent_ceiling_gb=50, child_ceiling_gb=40)
        with self.assertRaises(ParentVolumeError):
            await self._adjust_child(20)
        self.assertEqual(await self._child_total(), 40)

    async def test_raise_within_parent_room_is_saved(self) -> None:
        await self._tree(parent_ceiling_gb=100, child_ceiling_gb=40)
        await self._adjust_child(30)
        self.assertEqual(await self._child_total(), 70)

    async def test_lowering_a_child_always_passes(self) -> None:
        await self._tree(parent_ceiling_gb=30, child_ceiling_gb=40)  # already over
        await self._adjust_child(-5)
        self.assertEqual(await self._child_total(), 35)

    async def test_parent_sales_count_too(self) -> None:
        await self._tree(parent_ceiling_gb=100, child_ceiling_gb=40)
        async with self.Session() as session:
            session.add(BotUser(id=500, telegram_id=500, role=Role.USER.value, referral_code="c500"))
            session.add(UserService(bot_user_id=500, pg_username="u", shop_id=10, quota_data_limit_bytes=55 * GB))
            await session.commit()
        with self.assertRaises(ParentVolumeError):
            await self._adjust_child(10)

    async def test_extra_purchase_checks_before_the_charge(self) -> None:
        await self._tree(parent_ceiling_gb=50, child_ceiling_gb=40)
        async with self.Session() as session:
            with self.assertRaises(ParentVolumeError):
                await assert_child_growth_fits(session, pg_username="child-admin", add_gb=11)
            await assert_child_growth_fits(session, pg_username="child-admin", add_gb=10)

    async def test_record_unit_extra_is_checked(self) -> None:
        await self._tree(parent_ceiling_gb=50, child_ceiling_gb=40)
        async with self.Session() as session:
            with patch("app.services.pg_admin_subscription._assert_owner_capacity_budget", new=AsyncMock()):
                with self.assertRaises(ParentVolumeError):
                    await record_unit_extra(session, pg_username="child-admin", gb=11)

    async def test_disabled_child_frees_and_reenable_is_checked(self) -> None:
        _p, _c, _pp, child_pid = await self._tree(parent_ceiling_gb=100, child_ceiling_gb=40)
        async with self.Session() as session:
            child = await session.get(OrgPrincipal, child_pid)
            child.status = "disabled"
            await session.commit()
            self.assertEqual(await sum_allocated_bytes(session, 10), 0)
            session.add(BotUser(id=500, telegram_id=500, role=Role.USER.value, referral_code="c500"))
            session.add(UserService(bot_user_id=500, pg_username="u", shop_id=10, quota_data_limit_bytes=70 * GB))
            await session.commit()
            child.status = STATUS_ACTIVE
            await session.flush()
            with self.assertRaises(ParentVolumeError):
                await assert_parent_has_room_for_reenabled(session, child)

    async def test_uncapped_child_counts_zero_and_is_listed(self) -> None:
        await self._tree(parent_ceiling_gb=100, child_ceiling_gb=None)
        async with self.Session() as session:
            self.assertEqual(await sum_allocated_bytes(session, 10), 0)
            uncapped = await list_uncapped_children(session, 10)
            self.assertEqual([(c.shop_id, c.pg_username) for c in uncapped], [(20, "child-admin")])

    async def test_parent_that_allows_oversell_passes_anything(self) -> None:
        await self._tree(parent_ceiling_gb=50, child_ceiling_gb=40)
        async with self.Session() as session:
            parent = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == 10))
            parent.allow_volume_oversell = True
            await session.commit()
        await self._adjust_child(500)
        self.assertEqual(await self._child_total(), 540)

    async def test_other_parents_children_are_not_counted(self) -> None:
        await self._tree(parent_ceiling_gb=100, child_ceiling_gb=40)
        async with self.Session() as session:
            self.assertEqual(await sum_allocated_bytes(session, 20), 0)

    async def test_two_children_raising_at_once_cannot_both_fit(self) -> None:
        await self._tree(parent_ceiling_gb=100, child_ceiling_gb=40)
        async with self.Session() as session:
            session.add(BotUser(id=30, telegram_id=30, role=Role.RESELLER.value, referral_code="c2"))
            profile = ResellerProfile(user_id=30, pg_admin_username="child2", billing_mode="fixed",
                                      allow_volume_oversell=False, is_active=True)
            session.add(profile)
            await session.flush()
            parent = await session.scalar(select(OrgPrincipal).where(OrgPrincipal.depth == DEPTH_ONE))
            session.add(OrgPrincipal(parent_id=parent.id, depth=DEPTH_TWO, status=STATUS_ACTIVE,
                                     pg_username="child2", reseller_profile_id=profile.id))
            session.add(PgAdminSubscription(pg_username="child2", base_gb=10, extra_gb_purchased=0))
            await session.commit()

        async def raise_child(user_id: int) -> bool:
            async with self.Session() as session:
                profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == user_id))
                try:
                    await admin_adjust_reseller_subscription(session, profile, extra_gb=40)
                except ParentVolumeError:
                    return False
                return True

        # Patched once around both tasks: nested patches in concurrent tasks restore out of order.
        with patch("app.services.pg_admin_subscription.sync_pg_capacity_from_sub", new=AsyncMock()), patch(
            "app.services.pg_admin_subscription._assert_owner_capacity_budget", new=AsyncMock()
        ):
            results = await asyncio.gather(raise_child(20), raise_child(30))
        self.assertEqual(sorted(results), [False, True])


if __name__ == "__main__":
    unittest.main()
