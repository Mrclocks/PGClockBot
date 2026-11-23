"""PG DELETE 404 + confirmed GET 404 allows local service cleanup (secure)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("WEB_SECRET", "test-web-secret-for-pg-delete-404-32chars!!")

from app.services.pasarguard import PasarGuardError, is_pg_not_found
from tests.postgres_support import scoped_async_engine


class IsPgNotFoundTests(unittest.TestCase):
    def test_only_clean_404(self):
        self.assertTrue(is_pg_not_found(PasarGuardError("gone", 404)))
        self.assertFalse(is_pg_not_found(PasarGuardError("denied", 403)))
        self.assertFalse(is_pg_not_found(PasarGuardError("boom", 500)))
        self.assertFalse(is_pg_not_found(RuntimeError("pg down")))
        self.assertFalse(is_pg_not_found(TimeoutError("timeout")))


class EnsurePgUserRemovedTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_success(self):
        from app.services.bot_user_admin import ensure_pg_user_removed_or_absent

        pg = MagicMock()
        pg.delete_user_by_id = AsyncMock(return_value=None)
        pg.get_user_by_id = AsyncMock()
        out = await ensure_pg_user_removed_or_absent(pg, 300, staff=None)
        self.assertEqual(out, "deleted")
        pg.get_user_by_id.assert_not_awaited()

    async def test_delete_404_confirmed_absent(self):
        from app.services.bot_user_admin import ensure_pg_user_removed_or_absent

        pg = MagicMock()
        pg.delete_user_by_id = AsyncMock(
            side_effect=PasarGuardError("DELETE failed (404)", 404)
        )
        pg.get_user_by_id = AsyncMock(
            side_effect=PasarGuardError("GET failed (404)", 404)
        )
        out = await ensure_pg_user_removed_or_absent(pg, 300, staff=None)
        self.assertEqual(out, "already_absent")
        pg.get_user_by_id.assert_awaited_once_with(300)

    async def test_delete_404_but_get_still_finds_user_fail_closed(self):
        from app.services.bot_user_admin import ensure_pg_user_removed_or_absent

        pg = MagicMock()
        pg.delete_user_by_id = AsyncMock(
            side_effect=PasarGuardError("DELETE failed (404)", 404)
        )
        pg.get_user_by_id = AsyncMock(return_value={"id": 300, "username": "x"})
        pg.set_disabled_by_id = AsyncMock(return_value={"disabled": True})
        with self.assertRaises(ValueError) as ctx:
            await ensure_pg_user_removed_or_absent(pg, 300, staff=None)
        self.assertIn("هنوز موجود", str(ctx.exception))
        # Must not fall through to disable when presence is confirmed.
        pg.set_disabled_by_id.assert_not_awaited()

    async def test_delete_404_get_timeout_fail_closed(self):
        """DELETE 404 alone is never enough — GET uncertainty keeps the local row."""
        from app.services.bot_user_admin import ensure_pg_user_removed_or_absent

        pg = MagicMock()
        pg.delete_user_by_id = AsyncMock(
            side_effect=PasarGuardError("DELETE failed (404)", 404)
        )
        pg.get_user_by_id = AsyncMock(side_effect=TimeoutError("pg slow"))
        with self.assertRaises(ValueError) as ctx:
            await ensure_pg_user_removed_or_absent(pg, 300, staff=None)
        self.assertIn("پاسارگارد", str(ctx.exception))
        self.assertNotIn("هنوز موجود", str(ctx.exception))

    async def test_delete_404_get_403_fail_closed(self):
        """Permission-shaped GET must not be treated as confirmed absence."""
        from app.services.bot_user_admin import ensure_pg_user_removed_or_absent

        pg = MagicMock()
        pg.delete_user_by_id = AsyncMock(
            side_effect=PasarGuardError("DELETE failed (404)", 404)
        )
        pg.get_user_by_id = AsyncMock(
            side_effect=PasarGuardError("forbidden", 403)
        )
        with self.assertRaises(ValueError) as ctx:
            await ensure_pg_user_removed_or_absent(pg, 300, staff=None)
        self.assertIn("پاسارگارد", str(ctx.exception))
        self.assertNotIn("هنوز موجود", str(ctx.exception))

    async def test_network_error_fail_closed_keeps_raise(self):
        from app.services.bot_user_admin import ensure_pg_user_removed_or_absent

        pg = MagicMock()
        pg.delete_user_by_id = AsyncMock(side_effect=TimeoutError("down"))
        pg.set_disabled_by_id = AsyncMock(side_effect=TimeoutError("down"))
        with self.assertRaises(ValueError) as ctx:
            await ensure_pg_user_removed_or_absent(pg, 300, staff=None)
        self.assertIn("پاسارگارد", str(ctx.exception))

    async def test_disable_fallback_success(self):
        from app.services.bot_user_admin import ensure_pg_user_removed_or_absent

        pg = MagicMock()
        pg.delete_user_by_id = AsyncMock(side_effect=PasarGuardError("busy", 500))
        pg.set_disabled_by_id = AsyncMock(return_value={"disabled": True})
        out = await ensure_pg_user_removed_or_absent(pg, 300, staff=None)
        self.assertEqual(out, "disabled")

    async def test_no_disable_fallback_when_disallowed(self):
        from app.services.bot_user_admin import ensure_pg_user_removed_or_absent

        pg = MagicMock()
        pg.delete_user_by_id = AsyncMock(side_effect=PasarGuardError("busy", 500))
        pg.set_disabled_by_id = AsyncMock(return_value={"disabled": True})
        with self.assertRaises(ValueError):
            await ensure_pg_user_removed_or_absent(
                pg, 300, staff=None, allow_disable_fallback=False
            )
        pg.set_disabled_by_id.assert_not_awaited()


class AdminDeleteServiceOrphanTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_row_removed_when_pg_already_404(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.db.models import Base, BotUser, Role, UserService
        from app.services.bot_user_admin import admin_delete_service

        engine = scoped_async_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async with Session() as session:
            user = BotUser(telegram_id=9002, referral_code="U9002", role=Role.USER.value)
            session.add(user)
            await session.flush()
            svc = UserService(
                bot_user_id=user.id,
                pg_user_id=300,
                pg_username="gone300",
                remark="orphan",
            )
            session.add(svc)
            await session.commit()
            await session.refresh(svc)
            svc_id = int(svc.id)

            pg = MagicMock()
            pg.delete_user_by_id = AsyncMock(
                side_effect=PasarGuardError("DELETE /api/user/by-id/300 failed (404)", 404)
            )
            pg.get_user_by_id = AsyncMock(
                side_effect=PasarGuardError("GET failed (404)", 404)
            )
            staff = {"role": "admin", "pg_permissions": ["users.delete", "users.disable"]}

            with (
                patch(
                    "app.services.bot_user_admin._pg_client_for_bot_service",
                    AsyncMock(return_value=pg),
                ),
                patch(
                    "app.services.bot_user_admin.assert_staff_pg_user_action",
                    MagicMock(),
                ),
            ):
                info = await admin_delete_service(
                    session, svc, delete_pg=True, staff=staff
                )

            self.assertTrue(info["pg_deleted"])
            self.assertTrue(info["pg_already_absent"])
            self.assertIsNone(await session.get(UserService, svc_id))

        await engine.dispose()

    async def test_local_row_kept_on_transport_failure(self):
        """Regression: non-404 failures must still fail closed."""
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.db.models import Base, BotUser, Role, UserService
        from app.services.bot_user_admin import admin_delete_service

        engine = scoped_async_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async with Session() as session:
            user = BotUser(telegram_id=9003, referral_code="U9003", role=Role.USER.value)
            session.add(user)
            await session.flush()
            svc = UserService(
                bot_user_id=user.id,
                pg_user_id=4242,
                pg_username="u4242",
                remark="test",
            )
            session.add(svc)
            await session.commit()
            await session.refresh(svc)

            pg = MagicMock()
            pg.delete_user_by_id = AsyncMock(side_effect=RuntimeError("pg down"))
            pg.set_disabled_by_id = AsyncMock(side_effect=RuntimeError("pg down"))
            staff = {"role": "admin", "pg_permissions": ["users.delete", "users.disable"]}

            with (
                patch(
                    "app.services.bot_user_admin._pg_client_for_bot_service",
                    AsyncMock(return_value=pg),
                ),
                patch(
                    "app.services.bot_user_admin.assert_staff_pg_user_action",
                    MagicMock(),
                ),
            ):
                with self.assertRaises(ValueError):
                    await admin_delete_service(
                        session, svc, delete_pg=True, staff=staff
                    )

            still = await session.get(UserService, svc.id)
            self.assertIsNotNone(still)
            self.assertEqual(still.pg_user_id, 4242)

        await engine.dispose()


if __name__ == "__main__":
    unittest.main()
