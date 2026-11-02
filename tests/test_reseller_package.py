"""A shop owner renewing into their own or another package: what is offered, what is charged, once."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, PgAdminSubscription, ResellerPlan, ResellerProfile, WalletTransaction
from app.services.billing import GB
from app.services.reseller_package import (
    NEEDS_SUPPORT,
    RELATION_CURRENT,
    RELATION_DOWNGRADE,
    RELATION_UPGRADE,
    STALE_PAGE,
    USAGE_UNREADABLE,
    package_overview,
    renew_package,
)

PERMS = '["orders","payments"]'
BRONZE, SILVER, GOLD_PERMS, TINY, PAYG, OTHER_ROLE = 1, 2, 3, 4, 5, 6


def plan(plan_id: int, name: str, *, users: int, gb: int, price: int, **extra) -> ResellerPlan:
    fields = {
        "plan_kind": "subscription", "billing_mode": "fixed", "duration_days": 30, "web_permissions": PERMS,
        "pg_role_id": 2, "pg_group_ids": "1,2", "create_pg_admin": True, "is_active": True, "is_system": False,
        **extra,
    }
    return ResellerPlan(id=plan_id, name=name, included_users=users, included_gb=gb, price=price, sort_order=plan_id, **fields)


class ResellerPackageTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=3, telegram_id=33, referral_code="shop", role="reseller", wallet_balance=1_000_000),
            plan(BRONZE, "برنز", users=10, gb=100, price=100_000),
            plan(SILVER, "نقره", users=20, gb=200, price=200_000),
            plan(GOLD_PERMS, "طلا", users=50, gb=500, price=500_000, web_permissions='["orders","payments","campaigns"]'),
            plan(TINY, "کوچک", users=5, gb=50, price=50_000),
            plan(PAYG, "مصرفی", users=0, gb=0, price=0, billing_mode="payg"),
            plan(OTHER_ROLE, "نقش دیگر", users=20, gb=200, price=200_000, pg_role_id=9),
            ResellerProfile(id=1, user_id=3, pg_admin_username="shop1", plan_id=BRONZE, web_permissions=PERMS, is_active=True),
            PgAdminSubscription(pg_username="shop1", plan_id=BRONZE, access_status="active", base_users=10, base_gb=100, renew_generation=4),
        ])
        await self.session.commit()
        self.admin = {"total_users": 8, "used_traffic": 30 * GB}
        self.pg = SimpleNamespace(
            get_admin=AsyncMock(side_effect=lambda *_a, **_k: dict(self.admin)),
            modify_admin=AsyncMock(return_value={}),
            get_users=AsyncMock(return_value={"users": []}),
            set_disabled_by_id=AsyncMock(),
        )
        self.patches = [
            patch("app.services.pasarguard.get_pg", return_value=self.pg),
            patch("app.services.billing_suspend._set_admin_enabled", AsyncMock()),
            # Volume cascade between parent and child admins is not under test here.
            patch("app.services.pg_admin_subscription.assert_child_growth_fits", AsyncMock()),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        await self.session.close()
        await self.engine.dispose()

    async def profile(self) -> ResellerProfile:
        self.session.expire_all()
        return await self.session.get(ResellerProfile, 1)

    async def owner(self) -> BotUser:
        return await self.session.get(BotUser, 3)

    async def renew(self, plan_id: int, generation: int = 4) -> dict:
        profile = await self.profile()
        return await renew_package(
            self.session, owner=await self.owner(), profile=profile, plan_id=plan_id, generation=generation,
        )

    async def wallet(self) -> int:
        self.session.expire_all()
        return int((await self.session.get(BotUser, 3)).wallet_balance)

    async def debits(self) -> int:
        return int(await self.session.scalar(
            select(func.count()).select_from(WalletTransaction).where(WalletTransaction.user_id == 3, WalletTransaction.amount < 0)
        ))


class PackageOfferTests(ResellerPackageTestCase):
    async def test_offers_current_first_then_safe_moves(self) -> None:
        overview = await package_overview(self.session, await self.profile())
        self.assertEqual(overview.generation, 4)
        by_id = {int(c.plan.id): c for c in overview.choices}
        self.assertEqual(overview.choices[0].relation, RELATION_CURRENT)
        self.assertEqual((by_id[SILVER].relation, by_id[SILVER].blocked), (RELATION_UPGRADE, None))
        self.assertEqual(by_id[SILVER].invoice["total"], 200_000)
        self.assertEqual(by_id[GOLD_PERMS].blocked, NEEDS_SUPPORT, "different permissions need the admin")
        self.assertEqual(by_id[OTHER_ROLE].blocked, NEEDS_SUPPORT, "a different panel role needs the admin")
        self.assertIsNotNone(by_id[PAYG].blocked)
        self.assertEqual(by_id[TINY].relation, RELATION_DOWNGRADE)
        self.assertIn("8 کاربر", by_id[TINY].blocked, "8 users do not fit a 5-user package")

    async def test_smaller_package_is_offered_when_usage_fits(self) -> None:
        self.admin = {"total_users": 3, "used_traffic": 10 * GB}
        overview = await package_overview(self.session, await self.profile())
        tiny = next(c for c in overview.choices if int(c.plan.id) == TINY)
        self.assertIsNone(tiny.blocked)

    async def test_payg_shop_has_no_package_renewal(self) -> None:
        profile = await self.profile()
        profile.billing_mode = "payg"
        await self.session.commit()
        with self.assertRaises(ValueError):
            await package_overview(self.session, await self.profile())


class PackageRenewTests(ResellerPackageTestCase):
    async def test_renewing_the_current_package_charges_once_per_page(self) -> None:
        result = await self.renew(BRONZE)
        self.assertEqual((result["amount"], result["changed"]), (100_000, False))
        self.assertEqual(await self.wallet(), 900_000)
        with self.assertRaises(ValueError) as caught:
            await self.renew(BRONZE)
        self.assertEqual(str(caught.exception), STALE_PAGE, "a repeated tap with the old page renews nothing")
        self.assertEqual((await self.wallet(), await self.debits()), (900_000, 1))

    async def test_upgrade_moves_package_and_capacity(self) -> None:
        result = await self.renew(SILVER)
        self.assertTrue(result["changed"])
        self.assertEqual(await self.wallet(), 800_000)
        self.assertEqual((await self.profile()).plan_id, SILVER)
        sub = await self.session.scalar(select(PgAdminSubscription))
        self.assertEqual((sub.plan_id, sub.base_users, sub.base_gb), (SILVER, 20, 200))
        payload = self.pg.modify_admin.await_args.args[1]
        self.assertEqual((payload["max_users"], payload["data_limit"]), (20, 200 * GB))
        self.assertEqual((await self.profile()).web_permissions, PERMS, "permissions are never changed by a package move")

    async def test_downgrade_that_does_not_fit_is_refused_before_any_charge(self) -> None:
        with self.assertRaises(ValueError):
            await self.renew(TINY)
        self.assertEqual((await self.wallet(), (await self.profile()).plan_id), (1_000_000, BRONZE))
        self.pg.modify_admin.assert_not_awaited()

    async def test_downgrade_that_fits_is_applied(self) -> None:
        self.admin = {"total_users": 3, "used_traffic": 10 * GB}
        await self.renew(TINY)
        self.assertEqual((await self.wallet(), (await self.profile()).plan_id), (950_000, TINY))

    async def test_unreadable_usage_refuses_a_downgrade(self) -> None:
        self.pg.get_admin = AsyncMock(side_effect=TimeoutError("panel down"))
        with self.assertRaises(ValueError) as caught:
            await self.renew(TINY)
        self.assertEqual(str(caught.exception), USAGE_UNREADABLE)
        self.assertEqual(await self.wallet(), 1_000_000)

    async def test_packages_that_need_the_admin_are_refused_even_when_posted(self) -> None:
        for plan_id in (GOLD_PERMS, OTHER_ROLE, PAYG, 999):
            with self.subTest(plan_id=plan_id):
                with self.assertRaises(ValueError):
                    await self.renew(plan_id)
        self.assertEqual((await self.wallet(), (await self.profile()).plan_id), (1_000_000, BRONZE))

    async def test_short_wallet_changes_nothing(self) -> None:
        owner = await self.owner()
        owner.wallet_balance = 150_000
        await self.session.commit()
        with self.assertRaises(ValueError):
            await self.renew(SILVER)
        self.assertEqual((await self.wallet(), (await self.profile()).plan_id), (150_000, BRONZE))


if __name__ == "__main__":
    unittest.main()
