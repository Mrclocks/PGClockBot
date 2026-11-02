"""The owner grants the shop Mini App permission to selected resellers from the resellers page."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.shop_permission_grant import GrantResult, grant_shop_permission, grant_summary

OWNER = {
    "role": "admin",
    "id": 1,
    "username": "root",
    "org_principal_id": 1,
    "org_depth": 0,
    "org_parent_id": None,
    "org_status": "active",
    "web_owner": True,
}


class BulkGrantHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'grant.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _reseller(self, user_id: int, perms: str | None) -> int:
        from app.db.models import BotUser, ResellerProfile

        async with self.Session() as session:
            session.add(BotUser(id=user_id, telegram_id=user_id * 100, referral_code=f"r{user_id}", role="reseller"))
            await session.flush()
            profile = ResellerProfile(user_id=user_id, web_permissions=perms, bot_permissions=perms, is_active=True)
            session.add(profile)
            await session.commit()
            return int(profile.id)

    async def _perms(self, user_id: int) -> tuple[str | None, str | None]:
        from sqlalchemy import select

        from app.db.models import ResellerProfile

        async with self.Session() as session:
            row = (await session.execute(select(ResellerProfile).where(ResellerProfile.user_id == user_id))).scalar_one()
            return row.web_permissions, row.bot_permissions


class GrantServiceTests(BulkGrantHarness):
    async def test_grant_adds_the_key_and_leaves_others_alone(self):
        granted_id = await self._reseller(11, "dashboard,orders")
        await self._reseller(12, "dashboard,miniapp")
        await self._reseller(13, None)
        await self._reseller(14, "")
        async with self.Session() as session:
            result = await grant_shop_permission(session, [11, 12, 13, 14, 999], key="miniapp")
        self.assertEqual(result.granted_profile_ids, [granted_id])
        self.assertEqual(result.already_had, 2)
        self.assertEqual(result.skipped, 2)  # explicit empty list + unknown id
        self.assertEqual(await self._perms(11), ("dashboard,miniapp,orders", "dashboard,miniapp,orders"))
        self.assertEqual(await self._perms(12), ("dashboard,miniapp", "dashboard,miniapp"))
        self.assertEqual(await self._perms(13), (None, None))
        self.assertEqual(await self._perms(14), ("", ""))

    async def test_running_twice_grants_once(self):
        await self._reseller(21, "orders")
        async with self.Session() as session:
            first = await grant_shop_permission(session, [21], key="miniapp")
        async with self.Session() as session:
            second = await grant_shop_permission(session, [21], key="miniapp")
        self.assertEqual(len(first.granted_profile_ids), 1)
        self.assertEqual(second.granted_profile_ids, [])
        self.assertEqual(second.already_had, 1)

    async def test_an_edit_saved_meanwhile_is_not_overwritten(self):
        from sqlalchemy import update

        from app.db.models import ResellerProfile

        await self._reseller(31, "orders")
        async with self.Session() as session:
            original_execute = session.execute
            calls = {"n": 0}

            async def execute(statement, *args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 2:  # after the read, before the write
                    async with self.Session() as editor:
                        await editor.execute(
                            update(ResellerProfile)
                            .where(ResellerProfile.user_id == 31)
                            .values(web_permissions="tickets", bot_permissions="tickets")
                        )
                        await editor.commit()
                return await original_execute(statement, *args, **kwargs)

            with mock.patch.object(session, "execute", side_effect=execute):
                result = await grant_shop_permission(session, [31], key="miniapp")
        self.assertEqual(result.granted_profile_ids, [])
        self.assertEqual(result.skipped, 1)
        self.assertEqual(await self._perms(31), ("tickets", "tickets"))

    async def test_only_allow_listed_keys_can_be_granted_in_bulk(self):
        async with self.Session() as session:
            for key in ("payments", "payment_void", "shop_settings", "gift_codes", "nonsense"):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    await grant_shop_permission(session, [1], key=key)

    def test_summary_text(self):
        self.assertIn("ok", grant_summary(GrantResult(granted_profile_ids=[1], skipped=1)))
        self.assertIn("ok", grant_summary(GrantResult(already_had=2)))
        self.assertIn("err", grant_summary(GrantResult(skipped=3)))
        self.assertIn("err", grant_summary(GrantResult()))


class GrantRouteTests(BulkGrantHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app.api.bulk_pages import register_bulk_pages

        class NotAdmin(Exception):
            pass

        self.staff = dict(OWNER)

        async def get_db():
            async with self.Session() as session:
                yield session

        def require_admin():
            from app.services.platform_identity import is_explicit_owner_staff

            if self.staff.get("role") != "admin" or not is_explicit_owner_staff(self.staff):
                raise NotAdmin()
            return self.staff

        app = FastAPI()

        @app.exception_handler(NotAdmin)
        async def _denied(request, exc):
            from fastapi.responses import PlainTextResponse

            return PlainTextResponse("denied", status_code=403)

        register_bulk_pages(
            app, require_admin=require_admin, require_perm=lambda _p: (lambda: self.staff), get_db=get_db,
            require_staff=lambda: self.staff,
        )
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await super().asyncTearDown()

    async def test_owner_grants_and_the_menu_button_is_resynced(self):
        profile_id = await self._reseller(41, "orders")
        with mock.patch("app.api.bulk_pages.schedule_shop_menu_sync") as sync:
            response = await self.client.post("/resellers/bulk-action", data={"action": "grant_miniapp", "ids": ["41"]})
        self.assertEqual(response.status_code, 303)
        self.assertIn("ok=", response.headers["location"])
        sync.assert_called_once_with([profile_id])
        self.assertIn("miniapp", (await self._perms(41))[0])

    async def test_a_reseller_cannot_use_the_owner_bulk_action(self):
        await self._reseller(51, "orders,shop_settings")
        self.staff = {"role": "reseller", "bot_user_id": 51, "id": 51, "permissions": ["orders", "shop_settings"]}
        with mock.patch("app.api.bulk_pages.schedule_shop_menu_sync") as sync:
            response = await self.client.post("/resellers/bulk-action", data={"action": "grant_miniapp", "ids": ["51"]})
        self.assertEqual(response.status_code, 403)
        sync.assert_not_called()
        self.assertEqual(await self._perms(51), ("orders,shop_settings", "orders,shop_settings"))

    async def test_nothing_granted_does_not_resync(self):
        await self._reseller(61, "")
        with mock.patch("app.api.bulk_pages.schedule_shop_menu_sync") as sync:
            response = await self.client.post("/resellers/bulk-action", data={"action": "grant_miniapp", "ids": ["61"]})
        self.assertIn("err=", response.headers["location"])
        sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
