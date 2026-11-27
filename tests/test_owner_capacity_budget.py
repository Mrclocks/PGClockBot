"""The installer's panel budget counts only capacity still sold to a live shop or staff login."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import (
    Base,
    BotUser,
    OrgPrincipal,
    PgAdminSubscription,
    PgStaffAccess,
    ResellerProfile,
    Role,
)
from app.services.billing import GB
from app.services.org_principals import DEPTH_ONE, DEPTH_TWO, STATUS_ACTIVE
from app.services.pg_admin_subscription import admin_adjust_reseller_subscription
from tests.postgres_support import scoped_async_engine

OWNER = "owner"
OWNER_LIMIT_GB = 3000


class FakePanel:
    """Owner admin with a data limit; shop admins accept capacity writes."""

    def __init__(self) -> None:
        self.admins: dict[str, dict] = {OWNER: {"id": 1, "username": OWNER, "data_limit": OWNER_LIMIT_GB * GB}}
        self.writes: list[tuple[str, dict]] = []

    async def get_admin(self, username: str) -> dict:
        return self.admins.setdefault(username, {"id": len(self.admins) + 1, "username": username})

    async def modify_admin(self, username: str, payload: dict) -> dict:
        self.writes.append((username, payload))
        admin = await self.get_admin(username)
        admin.update(payload)
        return admin


class OwnerBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.panel = FakePanel()
        await self._add_shop(79, "shop79", base_gb=400, extra_gb=1600)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _add_shop(self, user_id: int, pg_username: str, *, base_gb: int, extra_gb: int = 0) -> int:
        async with self.Session() as session:
            session.add(BotUser(id=user_id, telegram_id=user_id, role=Role.RESELLER.value, referral_code=f"r{user_id}"))
            profile = ResellerProfile(
                user_id=user_id, pg_admin_username=pg_username, billing_mode="fixed",
                allow_volume_oversell=True, is_active=True,
            )
            session.add(profile)
            session.add(PgAdminSubscription(pg_username=pg_username, base_gb=base_gb, extra_gb_purchased=extra_gb))
            await session.commit()
            return int(profile.id)

    async def _add_leftover_subscription(self, pg_username: str, gb: int) -> None:
        async with self.Session() as session:
            session.add(PgAdminSubscription(pg_username=pg_username, base_gb=gb, extra_gb_purchased=0))
            await session.commit()

    async def _adjust(self, gb: int) -> None:
        settings = SimpleNamespace(pg_username=OWNER)
        with patch("app.services.pasarguard.get_pg", return_value=self.panel), patch(
            "app.config.get_settings", return_value=settings
        ):
            async with self.Session() as session:
                profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == 79))
                await admin_adjust_reseller_subscription(session, profile, extra_gb=gb)

    async def _shop79_total(self) -> int:
        async with self.Session() as session:
            sub = await session.scalar(select(PgAdminSubscription).where(PgAdminSubscription.pg_username == "shop79"))
            return int(sub.base_gb) + int(sub.extra_gb_purchased)

    async def test_raise_within_budget_writes_the_exact_panel_limit(self) -> None:
        await self._adjust(1000)
        self.assertEqual(await self._shop79_total(), 3000)
        self.assertEqual(self.panel.writes, [("shop79", {"data_limit": 3000 * GB})])

    async def test_plain_panel_admin_subscription_still_counts(self) -> None:
        await self._add_leftover_subscription("plain-admin", 900)
        with self.assertRaisesRegex(ValueError, "سقف حجم ادمین اصلی"):
            await self._adjust(200)
        self.assertEqual(await self._shop79_total(), 2000)

    async def test_sub_shop_volume_is_not_counted_twice(self) -> None:
        parent_profile_id = await self._add_shop(80, "parent80", base_gb=500)
        child_profile_id = await self._add_shop(81, "child81", base_gb=400)
        async with self.Session() as session:
            parent = OrgPrincipal(
                parent_id=None, depth=DEPTH_ONE, status=STATUS_ACTIVE,
                pg_username="parent80", reseller_profile_id=parent_profile_id,
            )
            session.add(parent)
            await session.flush()
            session.add(OrgPrincipal(
                parent_id=int(parent.id), depth=DEPTH_TWO, status=STATUS_ACTIVE,
                pg_username="child81", reseller_profile_id=child_profile_id,
            ))
            await session.commit()
        # 2000 + 500 (parent, covering the child) + 500 raise = 3000: fits only without the child's 400.
        await self._adjust(500)
        self.assertEqual(await self._shop79_total(), 2500)

    async def test_live_shop_capacity_still_refuses_an_oversized_raise(self) -> None:
        await self._add_shop(80, "shop80", base_gb=900)
        with self.assertRaisesRegex(ValueError, "سقف حجم ادمین اصلی"):
            await self._adjust(200)
        self.assertEqual(await self._shop79_total(), 2000)
        self.assertEqual(self.panel.writes, [])

    async def test_staff_login_capacity_still_counts(self) -> None:
        await self._add_leftover_subscription("staff-admin", 900)
        async with self.Session() as session:
            session.add(PgStaffAccess(pg_username="staff-admin", web_username="staff", web_password_hash="x"))
            await session.commit()
        with self.assertRaisesRegex(ValueError, "سقف حجم ادمین اصلی"):
            await self._adjust(200)
        self.assertEqual(await self._shop79_total(), 2000)

    async def test_lowering_is_never_refused(self) -> None:
        await self._add_shop(80, "shop80", base_gb=2500)
        await self._adjust(-300)
        self.assertEqual(await self._shop79_total(), 1700)
        self.assertEqual(self.panel.writes, [("shop79", {"data_limit": 1700 * GB})])


if __name__ == "__main__":
    unittest.main()
