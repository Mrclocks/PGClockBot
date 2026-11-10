"""Platform owner gets a purchase report when a reseller buys capacity/package from them."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, PgAdminSubscription, ResellerPlan, ResellerProfile
from app.services.billing import GB
from app.services.reseller_capacity import buy_extra_gb
from app.services.reseller_package import renew_package
from app.services.reseller_packs import buy_addon_pack
from app.services.reseller_purchase_notify import (
    NOTIFY_KEY,
    notify_capacity_wallet_purchase,
    notify_owner_of_reseller_purchase,
    notify_package_wallet_renew,
    pack_name_for_offer_reason,
)


class NotifyHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_notify_uses_platform_channel_and_purchase_pref(self) -> None:
        session = AsyncMock()
        buyer = SimpleNamespace(id=3, telegram_id=33, full_name="Shop", username="s")
        bot = MagicMock()
        bot.session = AsyncMock()
        dispatch = AsyncMock(return_value=2)
        with (
            patch("app.bot.create_bot", return_value=bot),
            patch("app.services.reseller_purchase_notify._dispatch_dual_notify", dispatch),
        ):
            n = await notify_owner_of_reseller_purchase(
                session,
                buyer=buyer,
                title="➕ خرید ظرفیت نمایندگی",
                detail_lines=["line"],
                amount=50_000,
            )
        self.assertEqual(n, 2)
        self.assertEqual(dispatch.await_args.args[2], NOTIFY_KEY)
        self.assertFalse(dispatch.await_args.kwargs.get("shop"))
        self.assertTrue(dispatch.await_args.kwargs.get("platform"))
        bot.session.close.assert_awaited()

    async def test_capacity_and_renew_titles(self) -> None:
        session = AsyncMock()
        buyer = SimpleNamespace(id=1, telegram_id=1, full_name="A", username=None)
        with patch(
            "app.services.reseller_purchase_notify.notify_owner_of_reseller_purchase",
            AsyncMock(return_value=1),
        ) as notify:
            await notify_capacity_wallet_purchase(
                session, buyer=buyer, amount=10, gb=5, users=0, pack_name="پک",
            )
            await notify_package_wallet_renew(
                session, buyer=buyer, amount=100, plan_name="برنز", changed=False,
            )
            await notify_package_wallet_renew(
                session, buyer=buyer, amount=200, plan_name="نقره", changed=True,
            )
        self.assertEqual(
            [c.kwargs["title"] for c in notify.await_args_list],
            ["➕ خرید ظرفیت نمایندگی", "🔄 تمدید بسته نمایندگی", "🔄 ارتقا بسته نمایندگی"],
        )


class WalletBuyNotifiesOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=3, telegram_id=33, referral_code="s3", role="reseller", wallet_balance=1_000_000),
                ResellerPlan(
                    id=1, name="برنز", plan_kind="subscription", billing_mode="fixed",
                    included_users=10, included_gb=100, price=100_000, duration_days=30,
                    allow_buy_extra=True, extra_gb_price=1_000, extra_user_price=2_000,
                    is_active=True, pg_role_id=2, pg_group_ids="1", create_pg_admin=True,
                    web_permissions='["orders"]',
                ),
                ResellerPlan(
                    id=11, name="پک ۲۰۰", plan_kind="addon_volume", price=200_000,
                    addon_gb=200, is_active=True,
                ),
                ResellerProfile(
                    id=1, user_id=3, pg_admin_username="shop3", plan_id=1, is_active=True,
                    web_permissions='["orders"]',
                ),
                PgAdminSubscription(
                    pg_username="shop3", plan_id=1, access_status="active",
                    base_users=10, base_gb=100, renew_generation=2,
                ),
            ])
            await session.commit()
        self.pg = SimpleNamespace(
            get_admin=AsyncMock(
                return_value={
                    "data_limit": 100 * GB, "max_users": 10, "total_users": 1, "used_traffic": 0,
                }
            ),
            modify_admin=AsyncMock(return_value={}),
            reset_admin_usage=AsyncMock(return_value={}),
            get_users=AsyncMock(return_value={"users": []}),
            set_disabled_by_id=AsyncMock(),
        )
        self.notify_cap = AsyncMock(return_value=1)
        self.notify_pkg = AsyncMock(return_value=1)
        self.patches = [
            patch("app.services.pasarguard.get_pg", return_value=self.pg),
            patch("app.services.reseller_capacity.assert_child_growth_fits", AsyncMock()),
            patch("app.services.pg_admin_subscription.assert_child_growth_fits", AsyncMock()),
            patch("app.services.billing_suspend._set_admin_enabled", AsyncMock()),
            patch(
                "app.services.reseller_purchase_notify.notify_capacity_wallet_purchase",
                self.notify_cap,
            ),
            patch(
                "app.services.reseller_purchase_notify.notify_package_wallet_renew",
                self.notify_pkg,
            ),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        await self.engine.dispose()

    async def test_extra_gb_notifies_the_platform_owner(self) -> None:
        async with self.Session() as session:
            user = await session.get(BotUser, 3)
            profile = await session.get(ResellerProfile, 1)
            plan = await session.get(ResellerPlan, 1)
            await buy_extra_gb(session, user=user, profile=profile, plan=plan, gb=5, generation=2)
        self.notify_cap.assert_awaited()
        kwargs = self.notify_cap.await_args.kwargs
        self.assertEqual((kwargs["gb"], kwargs["amount"]), (5, 5_000))

    async def test_addon_pack_notifies_the_platform_owner(self) -> None:
        async with self.Session() as session:
            user = await session.get(BotUser, 3)
            profile = await session.get(ResellerProfile, 1)
            plan = await session.get(ResellerPlan, 1)
            await buy_addon_pack(
                session, user=user, profile=profile, plan=plan, pack_id=11, generation=2,
            )
        self.notify_cap.assert_awaited()
        self.assertEqual(self.notify_cap.await_args.kwargs["pack_name"], "پک ۲۰۰")
        self.assertEqual(self.notify_cap.await_args.kwargs["amount"], 200_000)

    async def test_package_renew_notifies_the_platform_owner(self) -> None:
        async with self.Session() as session:
            user = await session.get(BotUser, 3)
            profile = await session.get(ResellerProfile, 1)
            await renew_package(session, owner=user, profile=profile, plan_id=1, generation=2)
        self.notify_pkg.assert_awaited()
        kwargs = self.notify_pkg.await_args.kwargs
        self.assertEqual(kwargs["plan_name"], "برنز")
        self.assertFalse(kwargs["changed"])
        self.assertEqual(kwargs["amount"], 100_000)

    async def test_pack_name_for_offer_reason(self) -> None:
        async with self.Session() as session:
            self.assertEqual(
                await pack_name_for_offer_reason(session, "reseller_addon:11:shop3"),
                "پک ۲۰۰",
            )
            self.assertIsNone(await pack_name_for_offer_reason(session, "reseller_extra_gb:5:shop3"))


if __name__ == "__main__":
    unittest.main()
