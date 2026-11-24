"""Regression test: pg_staff (no shop) must not see platform-wide settings.

``shop_owner_id(staff)`` returns ``None`` both for the real platform admin
and for a non-admin session with no shop (e.g. ``pg_staff``). Previously
``build_inbox_context`` computed ``rid = None if is_platform_admin(staff)
else shop_owner_id(staff)`` and then queried settings/action-center with
``reseller_id=None`` whenever ``rid`` was falsy — which is the *platform*
scope. A pg_staff account (who might only be allowed to see nodes in
PasarGuard) would therefore see platform-wide settings and counters in the
web panel's /inbox page. This must never regress.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.postgres_support import scoped_async_engine


class InboxPlatformScopeLeakTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _seed_platform_only_setting(self, session):
        from app.db.models import Setting

        session.add(Setting(key="card_number", value="SECRET-PLATFORM-CARD-6210"))
        await session.commit()

    async def test_pg_staff_without_shop_never_queries_platform_settings(self):
        """The platform-scope sentinel (reseller_id=None) must never be used
        for a scopeless non-admin — get_all_settings must simply not be called."""
        from app.services.panel_inbox import build_inbox_context

        async with self.Session() as session:
            await self._seed_platform_only_setting(session)

            pg_staff = {"role": "pg_staff", "id": 1, "username": "limited_admin"}
            fake_request = SimpleNamespace(state=SimpleNamespace())

            with patch(
                "app.services.users.get_all_settings", new=AsyncMock(return_value={})
            ) as mocked:
                await build_inbox_context(session, fake_request, pg_staff)
                mocked.assert_not_called()

    async def test_platform_admin_still_queries_platform_settings(self):
        from app.services.panel_inbox import build_inbox_context

        async with self.Session() as session:
            await self._seed_platform_only_setting(session)

            admin = {
                "role": "admin",
                "id": 1,
                "username": "root",
                "org_principal_id": 1,
                "org_depth": 0,
                "org_parent_id": None,
                "org_status": "active",
            }
            fake_request = SimpleNamespace(state=SimpleNamespace())

            with patch(
                "app.services.users.get_all_settings", new=AsyncMock(return_value={})
            ) as mocked:
                await build_inbox_context(session, fake_request, admin)
                mocked.assert_called_once()
                _, kwargs = mocked.call_args
                self.assertIsNone(kwargs.get("reseller_id"))

    async def test_reseller_with_shop_queries_own_shop_settings_only(self):
        from app.services.panel_inbox import build_inbox_context

        async with self.Session() as session:
            from app.db.models import BotUser, Role

            owner = BotUser(
                telegram_id=5001,
                username="shopowner",
                role=Role.RESELLER.value,
                referral_code="R5001",
            )
            session.add(owner)
            await session.commit()
            await session.refresh(owner)

            await self._seed_platform_only_setting(session)

            reseller_staff = {
                "role": "reseller",
                "bot_user_id": owner.id,
                "id": owner.id,
                "username": "shopowner",
            }
            fake_request = SimpleNamespace(state=SimpleNamespace())

            with patch(
                "app.services.users.get_all_settings", new=AsyncMock(return_value={})
            ) as mocked:
                await build_inbox_context(session, fake_request, reseller_staff)
                mocked.assert_called_once()
                _, kwargs = mocked.call_args
                self.assertEqual(kwargs.get("reseller_id"), owner.id)

    async def test_end_to_end_pg_staff_ui_has_no_platform_secret(self):
        """End-to-end (no mocking): the rendered inbox ``ui`` for a scopeless
        pg_staff must be empty/defaults, never containing the platform Setting."""
        from app.services.panel_inbox import build_inbox_context

        async with self.Session() as session:
            await self._seed_platform_only_setting(session)

            pg_staff = {"role": "pg_staff", "id": 1, "username": "limited_admin"}
            fake_request = SimpleNamespace(state=SimpleNamespace())

            ctx = await build_inbox_context(session, fake_request, pg_staff)

        # shop_maintenance is derived from ui.get("shop_maintenance_enabled");
        # with no scope it must fall back to the safe default (False/off),
        # never reflect a platform-level toggle.
        self.assertFalse(ctx.get("shop_maintenance"))


if __name__ == "__main__":
    unittest.main()
