"""Regression: user delete must succeed even when loyalty/terms FK rows exist.

Root cause of «کاربر حذف نمی‌شود»: delete_bot_user never cleaned
terms_acceptances / points_transactions / referral_events / lucky_wheel_* /
reward_redemptions / … so Postgres (and SQLite with FK=ON) raised
IntegrityError on DELETE FROM bot_users and the row stayed.

Previous agents patched the panel «reason» field; that never reached this path.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.postgres_support import scoped_async_engine


class DeleteBotUserFkCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_delete_succeeds_with_terms_and_points_rows(self):
        from app.db.models import BotUser, PointsTransaction, Role, TermsAcceptance
        from app.services.users import delete_bot_user

        async with self.Session() as session:
            user = BotUser(
                telegram_id=940001,
                username="fk_user",
                full_name="کاربر FK",
                role=Role.USER.value,
                referral_code="FK940001",
                wallet_balance=1000,
                points_balance=50,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            uid = user.id

            session.add(
                TermsAcceptance(
                    bot_user_id=uid,
                    shop_owner_id=0,
                    gate="entry",
                    content_hash="abc123",
                )
            )
            session.add(
                PointsTransaction(
                    user_id=uid,
                    amount=50,
                    balance_after=50,
                    tx_type="earn",
                    source="test",
                    description="seed",
                    idempotency_key=f"seed-{uid}",
                )
            )
            await session.commit()

            with patch("app.services.users.get_settings") as gs:
                gs.return_value.admin_ids = []
                with patch("app.services.pasarguard.get_pg") as gpg:
                    pg = AsyncMock()
                    gpg.return_value = pg
                    info = await delete_bot_user(session, uid)

            self.assertEqual(info["user_id"], uid)
            self.assertIsNone(await session.get(BotUser, uid))
            terms_left = (
                await session.execute(
                    select(TermsAcceptance).where(TermsAcceptance.bot_user_id == uid)
                )
            ).scalars().all()
            self.assertEqual(terms_left, [])
            pts_left = (
                await session.execute(
                    select(PointsTransaction).where(PointsTransaction.user_id == uid)
                )
            ).scalars().all()
            self.assertEqual(pts_left, [])

    async def test_delete_without_fk_cleanup_would_fail(self):
        """Document the failure mode: DELETE bot_users with orphan terms row."""
        from app.db.models import BotUser, Role, TermsAcceptance
        from sqlalchemy import text

        async with self.Session() as session:
            user = BotUser(
                telegram_id=940002,
                role=Role.USER.value,
                referral_code="FK940002",
                wallet_balance=0,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            session.add(
                TermsAcceptance(
                    bot_user_id=user.id,
                    shop_owner_id=0,
                    gate="entry",
                    content_hash="x",
                )
            )
            await session.commit()
            uid = user.id

            with self.assertRaises(Exception):
                await session.execute(
                    text("DELETE FROM bot_users WHERE id = :id"), {"id": uid}
                )
                await session.commit()
            await session.rollback()
            still = await session.get(BotUser, uid)
            self.assertIsNotNone(still)


class DeleteBotUserSourceGuards(unittest.TestCase):
    def test_delete_bot_user_cleans_loyalty_fks(self):
        src = Path("app/services/users.py").read_text(encoding="utf-8")
        block = src.split("async def delete_bot_user", 1)[1].split(
            "\ndef friendly_user_delete_error", 1
        )[0]
        for needle in (
            "TermsAcceptance",
            "PointsTransaction",
            "ReferralEvent",
            "LuckyWheelSpin",
            "LuckyWheelUserState",
            "RewardRedemption",
            "LoyaltyDiscountEntitlement",
            "OrgPrincipal",
            "PlanCategory",
            "ServiceAddonPack",
        ):
            self.assertIn(needle, block, msg=f"missing cleanup for {needle}")
        # Order: loyalty cleanup before UserService delete
        self.assertLess(
            block.find("delete(TermsAcceptance)"),
            block.find("delete(UserService)"),
        )
        self.assertLess(
            block.find("delete(PlanCategory)"),
            block.find("session.delete(user)"),
        )
        self.assertLess(
            block.find("delete(ServiceAddonPack)"),
            block.find("session.delete(user)"),
        )


if __name__ == "__main__":
    unittest.main()
