"""Removing a shop follows PasarGuard: no shop is removed here while its admin stays there.

The connected PasarGuard account must be owner or hold ``admins.delete``. A refusal or
an unknown outcome from PasarGuard leaves the shop, its sub-shops and the user's role
exactly as they were; an admin that is already gone (404) lets the cleanup finish.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.pasarguard import PasarGuardError
from app.services.resellers import (
    PANEL_ADMIN_DELETE_PARTIAL_MSG,
    PANEL_ADMIN_DELETE_REFUSED_MSG,
    PanelAdminNotRemoved,
    assert_platform_may_delete_pg_admins,
    revoke_reseller,
)

ALLOW_CHECK = "app.services.resellers.assert_platform_may_delete_pg_admins"


class RevokeResellerPanelAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app.db import Base
        import app.db.models  # noqa: F401  (registers every table on Base.metadata)

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'revoke.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _seed_shop(
        self, *, with_child: bool = False, pg_admins: tuple[bool, bool] = (True, True)
    ) -> tuple[int, list[int]]:
        from app.db.models import BotUser, ResellerProfile, Role
        from app.services.org_principals import (
            DEPTH_ONE,
            DEPTH_TWO,
            STATUS_ACTIVE,
            create_principal,
            ensure_owner_principal,
        )

        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            parent_id = int(owner.id)
            uids: list[int] = []
            profile_ids: list[int] = []
            levels = ((DEPTH_ONE, 960001, pg_admins[0]), (DEPTH_TWO, 960002, pg_admins[1]))
            for depth, tg, has_pg_admin in levels[: 2 if with_child else 1]:
                user = BotUser(telegram_id=tg, username=f"s{tg}", full_name="shop",
                               role=Role.RESELLER.value, referral_code=f"RS{tg}")
                session.add(user)
                await session.flush()
                pg_name = f"pg{tg}" if has_pg_admin else None
                profile = ResellerProfile(user_id=int(user.id), pg_admin_username=pg_name, is_active=True)
                session.add(profile)
                await session.flush()
                principal = await create_principal(
                    session, parent_id=parent_id, depth=depth, pg_username=f"pg{tg}",
                    reseller_profile_id=int(profile.id), bot_user_id=int(user.id), status=STATUS_ACTIVE,
                )
                parent_id = int(principal.id)
                uids.append(int(user.id))
                profile_ids.append(int(profile.id))
            await session.commit()
        return uids[0], profile_ids

    async def _assert_untouched(self, uid: int, profile_ids: list[int]) -> None:
        from app.db.models import BotUser, ResellerProfile, Role

        async with self.Session() as session:
            for pid in profile_ids:
                self.assertIsNotNone(await session.get(ResellerProfile, pid))
            self.assertEqual((await session.get(BotUser, uid)).role, Role.RESELLER.value)

    async def _revoke_with(self, uid: int, delete_admin: AsyncMock) -> dict:
        pg = AsyncMock()
        pg.delete_admin = delete_admin
        with patch("app.services.pasarguard.get_pg", return_value=pg), patch(ALLOW_CHECK, new=AsyncMock()):
            async with self.Session() as session:
                return await revoke_reseller(session, uid, delete_pg_admin=True, reason="test")

    async def test_refused_delete_keeps_the_shop(self) -> None:
        uid, profiles = await self._seed_shop()
        with self.assertRaises(PanelAdminNotRemoved) as ctx:
            await self._revoke_with(uid, AsyncMock(side_effect=PasarGuardError("no", 403)))
        self.assertEqual(str(ctx.exception), PANEL_ADMIN_DELETE_REFUSED_MSG)
        await self._assert_untouched(uid, profiles)

    async def test_unknown_outcome_keeps_the_shop(self) -> None:
        uid, profiles = await self._seed_shop()
        with self.assertRaises(PanelAdminNotRemoved):
            await self._revoke_with(uid, AsyncMock(side_effect=httpx.ReadTimeout("slow")))
        await self._assert_untouched(uid, profiles)

    async def test_sub_shop_refusal_keeps_parent_and_sub_shop_and_asks_for_a_retry(self) -> None:
        uid, profiles = await self._seed_shop(with_child=True)
        refuse_child = AsyncMock(side_effect=[None, PasarGuardError("no", 403)])
        with self.assertRaises(PanelAdminNotRemoved) as ctx:
            await self._revoke_with(uid, refuse_child)
        # The parent's admin is already gone from PasarGuard, so the message asks for a retry.
        self.assertEqual(str(ctx.exception), PANEL_ADMIN_DELETE_PARTIAL_MSG)
        await self._assert_untouched(uid, profiles)

    async def test_shop_without_panel_admin_never_touches_pasarguard(self) -> None:
        from app.db.models import ResellerProfile

        uid, profiles = await self._seed_shop(pg_admins=(False, False))
        probe = AsyncMock(return_value={"ok": False})
        pg = AsyncMock()
        with (
            patch("app.services.resellers.resolve_platform_pg_capabilities", new=probe),
            patch("app.services.pasarguard.get_pg", return_value=pg),
        ):
            async with self.Session() as session:
                info = await revoke_reseller(session, uid, delete_pg_admin=True, reason="test")
        self.assertFalse(info["pg_admin_deleted"])
        probe.assert_not_awaited()
        pg.delete_admin.assert_not_awaited()
        async with self.Session() as session:
            self.assertIsNone(await session.get(ResellerProfile, profiles[0]))

    async def test_sub_shop_with_panel_admin_still_needs_the_permission(self) -> None:
        uid, profiles = await self._seed_shop(with_child=True, pg_admins=(False, True))
        caps = {"ok": True, "pg_is_owner": False, "features": [],
                "role": {"is_owner": False, "permissions": {"admins": {"read": True}}}}
        pg = AsyncMock()
        with (
            patch("app.services.resellers.resolve_platform_pg_capabilities", new=AsyncMock(return_value=caps)),
            patch("app.services.pasarguard.get_pg", return_value=pg),
        ):
            async with self.Session() as session:
                with self.assertRaises(PanelAdminNotRemoved):
                    await revoke_reseller(session, uid, delete_pg_admin=True, reason="test")
        pg.delete_admin.assert_not_awaited()
        await self._assert_untouched(uid, profiles)

    async def test_admin_already_gone_finishes_the_cleanup(self) -> None:
        from app.db.models import ResellerProfile

        uid, profiles = await self._seed_shop()
        info = await self._revoke_with(uid, AsyncMock(side_effect=PasarGuardError("missing", 404)))
        self.assertFalse(info["pg_admin_deleted"])
        async with self.Session() as session:
            self.assertIsNone(await session.get(ResellerProfile, profiles[0]))

    async def test_account_without_admins_delete_is_refused_before_any_call(self) -> None:
        uid, profiles = await self._seed_shop()
        caps = {"ok": True, "pg_is_owner": False, "features": ["pg_users"],
                "role": {"is_owner": False, "permissions": {"admins": {"read": True, "update": True}}}}
        pg = AsyncMock()
        with (
            patch("app.services.resellers.resolve_platform_pg_capabilities", new=AsyncMock(return_value=caps)),
            patch("app.services.pasarguard.get_pg", return_value=pg),
        ):
            async with self.Session() as session:
                with self.assertRaises(PanelAdminNotRemoved):
                    await revoke_reseller(session, uid, delete_pg_admin=True, reason="test")
        pg.delete_admin.assert_not_awaited()
        await self._assert_untouched(uid, profiles)


class DeleteCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def _check(self, caps: dict) -> None:
        with patch("app.services.resellers.resolve_platform_pg_capabilities", new=AsyncMock(return_value=caps)):
            await assert_platform_may_delete_pg_admins()

    async def test_owner_and_admins_delete_pass(self) -> None:
        await self._check({"ok": True, "pg_is_owner": True, "features": [], "role": {"is_owner": True}})
        await self._check({"ok": True, "pg_is_owner": False, "features": [],
                           "role": {"is_owner": False, "permissions": {"admins": {"delete": True}}}})

    async def test_unreadable_account_is_refused(self) -> None:
        with self.assertRaises(PanelAdminNotRemoved):
            await self._check({"ok": False})


if __name__ == "__main__":
    unittest.main()
