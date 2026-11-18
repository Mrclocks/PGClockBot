"""Regression: PAYG reseller delete must not HTTP 500 / IntegrityError.

Root causes (v0.2.6):
1. notify_reseller_revoked after revoke ran outside try/except and could raise
   from notify_account_edit → unhandled 500 on /resellers/{id}/delete.
2. delete_bot_user never cleaned PlanCategory / ServiceAddonPack rows owned by
   the shop (owner_reseller_id FK, no CASCADE) — common on active PAYG shops.
"""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.postgres_support import scoped_async_engine


class NotifyResellerRevokedNeverRaises(unittest.IsolatedAsyncioTestCase):
    async def test_session_path_swallows_notify_failure(self):
        from app.db.models import BotUser, Role
        from app.services.resellers import notify_reseller_revoked

        user = BotUser(
            id=1,
            telegram_id=960001,
            role=Role.USER.value,
            referral_code="NR960001",
        )
        with patch(
            "app.services.notifications.notify_account_edit",
            new_callable=AsyncMock,
            side_effect=RuntimeError("telegram down"),
        ):
            ok = await notify_reseller_revoked(
                960001,
                "تست حذف نمایندگی",
                session=AsyncMock(),
                user=user,
                actor="admin",
            )
        self.assertFalse(ok)

    async def test_legacy_path_swallows_send_failure(self):
        from app.services.resellers import notify_reseller_revoked

        bot = AsyncMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("network"))
        bot.session.close = AsyncMock()
        with patch("app.bot.create_bot", return_value=bot):
            ok = await notify_reseller_revoked(960002, "علت تستی")
        self.assertFalse(ok)


class PaygResellerFullDeleteFkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def test_delete_payg_reseller_with_catalog_and_billing(self):
        from app.db.models import (
            BotUser,
            Plan,
            PlanCategory,
            ResellerBillingTransaction,
            ResellerProfile,
            Role,
            ServiceAddonPack,
        )
        from app.services.billing import BILLING_MODE_PAYG
        from app.services.users import delete_bot_user

        async with self.Session() as session:
            user = BotUser(
                telegram_id=960010,
                username="payg_shop",
                full_name="فروشگاه PAYG",
                role=Role.RESELLER.value,
                referral_code="PG960010",
                wallet_balance=50_000,
            )
            session.add(user)
            await session.flush()
            session.add(
                ResellerProfile(
                    user_id=int(user.id),
                    pg_admin_username="payg_shop_pg",
                    is_active=True,
                    billing_mode=BILLING_MODE_PAYG,
                    payg_wallet_linked=True,
                    billing_balance=50_000,
                )
            )
            cat = PlanCategory(
                name="ویژه",
                audience="users",
                owner_reseller_id=int(user.id),
                is_active=True,
            )
            session.add(cat)
            await session.flush()
            session.add(
                Plan(
                    name="ماهانه",
                    price=100_000,
                    duration_days=30,
                    owner_reseller_id=int(user.id),
                    category_id=int(cat.id),
                    is_active=True,
                )
            )
            session.add(
                ServiceAddonPack(
                    name="۱۰ گیگ",
                    kind="volume",
                    amount=10.0,
                    price=20_000,
                    owner_reseller_id=int(user.id),
                    is_active=True,
                )
            )
            session.add(
                ResellerBillingTransaction(
                    reseller_user_id=int(user.id),
                    kind="topup",
                    amount=50_000,
                    balance_after=50_000,
                    idempotency_key=f"payg-seed-{user.id}",
                )
            )
            await session.commit()
            uid = int(user.id)

            mock_pg = AsyncMock()
            mock_pg.delete_admin = AsyncMock(return_value=None)
            with patch("app.services.users.get_settings") as gs:
                gs.return_value.admin_ids = []
                with patch("app.services.pasarguard.get_pg", return_value=mock_pg):
                    with (
                        patch("app.services.reseller_bots.get_reseller_bot_manager", return_value=None),
                        patch("app.services.resellers.assert_platform_may_delete_pg_admins", new=AsyncMock()),
                    ):
                        info = await delete_bot_user(session, uid)

            self.assertEqual(info["user_id"], uid)
            self.assertIsNone(await session.get(BotUser, uid))
            cats = (
                await session.execute(
                    select(PlanCategory).where(PlanCategory.owner_reseller_id == uid)
                )
            ).scalars().all()
            self.assertEqual(cats, [])
            packs = (
                await session.execute(
                    select(ServiceAddonPack).where(
                        ServiceAddonPack.owner_reseller_id == uid
                    )
                )
            ).scalars().all()
            self.assertEqual(packs, [])
            txs = (
                await session.execute(
                    select(ResellerBillingTransaction).where(
                        ResellerBillingTransaction.reseller_user_id == uid
                    )
                )
            ).scalars().all()
            self.assertEqual(txs, [])


if __name__ == "__main__":
    unittest.main()
