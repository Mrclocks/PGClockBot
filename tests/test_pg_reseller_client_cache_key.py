"""Regression test: the cached reseller PasarGuard client must be keyed by
(reseller_id, pg_admin_username), not just reseller_id.

Previously ``get_pg_for_reseller`` cached clients purely by
``reseller_user_id``. If an operator re-linked a reseller's shop to a
*different* PasarGuard admin, the stale cached client (still authenticated
as the OLD admin) kept being reused until the process restarted or the token
happened to expire — silently performing operations under the wrong
PasarGuard identity/permissions instead of the newly-assigned admin's.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker

import app.services.pasarguard as pasarguard_mod
from tests.postgres_support import scoped_async_engine


class PgResellerClientCacheKeyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        pasarguard_mod.reset_pg()

    async def asyncTearDown(self):
        pasarguard_mod.reset_pg()
        await self.engine.dispose()

    async def _make_reseller_profile(self, session, *, user_id: int, pg_username: str):
        from app.db.models import BotUser, ResellerProfile, Role
        from app.services.secret_box import encrypt_secret

        owner = BotUser(
            id=user_id,
            telegram_id=user_id,
            username=f"owner{user_id}",
            role=Role.RESELLER.value,
            referral_code=f"R{user_id}",
        )
        session.add(owner)
        profile = ResellerProfile(
            user_id=user_id,
            pg_admin_username=pg_username,
            pg_admin_password_enc=encrypt_secret("s3cret-pass"),
        )
        session.add(profile)
        await session.commit()

    async def test_relinking_admin_username_bypasses_stale_cached_client(self):
        from app.services.pasarguard import get_pg_for_reseller

        async with self.Session() as session:
            await self._make_reseller_profile(session, user_id=777, pg_username="admin_a")

            with patch.object(
                pasarguard_mod.PasarGuardClient, "ensure_token", new=AsyncMock()
            ):
                client_a = await get_pg_for_reseller(session, 777)
                client_a._token = "tok-a"  # simulate a live/valid session

            # Operator re-links the shop to a different PG admin.
            from sqlalchemy import select

            from app.db.models import ResellerProfile
            from app.services.secret_box import encrypt_secret

            profile = (
                await session.execute(
                    select(ResellerProfile).where(ResellerProfile.user_id == 777)
                )
            ).scalar_one()
            profile.pg_admin_username = "admin_b"
            profile.pg_admin_password_enc = encrypt_secret("other-pass")
            await session.commit()

            with patch.object(
                pasarguard_mod.PasarGuardClient, "ensure_token", new=AsyncMock()
            ):
                client_b = await get_pg_for_reseller(session, 777)

        self.assertIsNot(
            client_b,
            client_a,
            "re-linking a reseller to a different PG admin must never reuse "
            "the previous admin's cached, still-authenticated client",
        )
        self.assertEqual(client_b._login_username, "admin_b")

    async def test_same_admin_username_reuses_cached_client(self):
        from app.services.pasarguard import get_pg_for_reseller

        async with self.Session() as session:
            await self._make_reseller_profile(session, user_id=888, pg_username="admin_same")

            with patch.object(
                pasarguard_mod.PasarGuardClient, "ensure_token", new=AsyncMock()
            ):
                client_1 = await get_pg_for_reseller(session, 888)
                client_1._token = "tok-1"
                client_2 = await get_pg_for_reseller(session, 888)

        self.assertIs(client_1, client_2)


if __name__ == "__main__":
    unittest.main()
