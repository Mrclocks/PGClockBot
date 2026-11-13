"""«دسترسی به پاسارگارد»: a closed shop gets no PasarGuard pages, actions or panel password."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.pg_panel_access_pages import access_target_from_form, apply_access_before_save
from app.db import Base
from app.db.models import BotUser, OrgPrincipal, ResellerPlan, ResellerProfile
from app.services.authz import authz_from_staff, can_pg_page, can_pg_user_action
from app.services.panel_nav import build_sidebar, viewer_from_context
from app.services.pasarguard import PasarGuardError
from app.services.pg_access import PG_FEATURE_KEYS
from app.services.pg_panel_access import (
    API_KEY_STILL_VALID_MESSAGE,
    ROTATION_FAILED_MESSAGE,
    RotationOutcome,
    close_access_warning,
    apply_pg_panel_access,
    finish_pg_password_rotation,
    is_pg_panel_blocked,
    may_share_pg_credentials,
    set_pg_panel_access,
)
from app.services.principal_web_identity import _attach_linked_shop_package
from app.services.resellers import (
    apply_reseller_panel_password,
    format_credentials_message,
    provision_reseller,
)
from app.services.secret_box import decrypt_secret, encrypt_secret

ROOT = Path(__file__).resolve().parents[1]
ALL_PG_USER_ACTIONS = {"create": True, "update": True, "delete": True, "reset": True, "revoke": True}


def _reseller_staff() -> dict:
    return {
        "role": "reseller",
        "permissions": ["dashboard", "plans"],
        "pg_permissions": list(PG_FEATURE_KEYS),
        "pg_actions": {"users": {"create": True}, "hosts": {"create": True}},
        "pg_user_actions": dict(ALL_PG_USER_ACTIONS),
        "pg_writes": {"users": True, "hosts": True},
        "pg_role": {"id": 3, "name": "seller"},
        "pg_access": {"max_users": 10},
    }


class SessionClampTests(unittest.TestCase):
    def test_closed_shop_session_has_no_pasarguard_page_or_action(self) -> None:
        staff = apply_pg_panel_access(_reseller_staff(), SimpleNamespace(allow_pg_panel_access=False))
        ctx = authz_from_staff(staff)
        for key in PG_FEATURE_KEYS:
            self.assertFalse(can_pg_page(ctx, key), key)
        for action in ALL_PG_USER_ACTIONS:
            self.assertFalse(can_pg_user_action(ctx, action), action)
        self.assertFalse(staff["pg_writes"])
        self.assertNotIn("pg_role", staff)
        self.assertTrue(is_pg_panel_blocked(staff))
        # Shop permissions and quota limits are not PasarGuard panel access.
        self.assertEqual(staff["permissions"], ["dashboard", "plans"])
        self.assertEqual(staff["pg_access"], {"max_users": 10})

    def test_open_shop_session_keeps_its_role_capabilities(self) -> None:
        staff = apply_pg_panel_access(_reseller_staff(), SimpleNamespace(allow_pg_panel_access=True))
        ctx = authz_from_staff(staff)
        self.assertTrue(can_pg_page(ctx, "pg_users"))
        self.assertFalse(is_pg_panel_blocked(staff))

    def test_stale_blocked_flag_is_dropped_when_access_reopens(self) -> None:
        stale = dict(_reseller_staff(), pg_panel_blocked=True)
        staff = apply_pg_panel_access(stale, SimpleNamespace(allow_pg_panel_access=True))
        self.assertFalse(is_pg_panel_blocked(staff))

    def test_closed_shop_sidebar_shows_no_pasarguard_item(self) -> None:
        staff = apply_pg_panel_access(_reseller_staff(), SimpleNamespace(allow_pg_panel_access=False))
        viewer = viewer_from_context(staff, {}, is_owner=False)
        paths = [item.href for section in build_sidebar(viewer, path="/home") for item in section.items]
        self.assertFalse([p for p in paths if p.startswith("/pg")], paths)
        open_viewer = viewer_from_context(_reseller_staff(), {}, is_owner=False)
        open_paths = [i.href for s in build_sidebar(open_viewer, path="/home") for i in s.items]
        self.assertTrue([p for p in open_paths if p.startswith("/pg")])

    def test_credentials_are_shared_only_while_access_is_open(self) -> None:
        self.assertFalse(may_share_pg_credentials(allow_access=False, share_panel_url=True))
        self.assertTrue(may_share_pg_credentials(allow_access=True, share_panel_url=True))
        self.assertFalse(may_share_pg_credentials(allow_access=True, share_panel_url=False))

    def test_closed_credentials_message_names_no_pasarguard_panel(self) -> None:
        text = format_credentials_message({
            "pg_panel_access": False,
            "pg_username": "shop_ab12",
            "pg_panel_url": "https://pg.example",
            "share_pg_panel_url": True,
            "unified_credentials": True,
            "web_username": "shop_ab12",
            "web_password": "Web-Secret-1",
            "panel_url": "https://panel.example",
        })
        self.assertNotIn("pg.example", text)
        self.assertNotIn("پاسارگارد", text)


class EditFormTests(unittest.TestCase):
    def test_form_without_the_switch_keeps_the_current_access(self) -> None:
        profile = SimpleNamespace(allow_pg_panel_access=True)
        self.assertTrue(access_target_from_form({}, profile))
        self.assertTrue(access_target_from_form({"pg_access_shown": "1", "allow_pg_panel_access": "1"}, profile))
        self.assertFalse(access_target_from_form({"pg_access_shown": "1"}, profile))

    def test_closing_access_turns_off_link_sharing(self) -> None:
        profile = SimpleNamespace(allow_pg_panel_access=True, share_pg_panel_url=True, pg_password_rotation_enc=None)
        apply_access_before_save(profile, target=False, share_panel_url=True)
        self.assertFalse(profile.share_pg_panel_url)
        # Closing itself (with the password change) runs only after the edit commits.
        self.assertTrue(profile.allow_pg_panel_access)

    def test_reopening_happens_inside_the_edit_and_drops_a_pending_change(self) -> None:
        profile = SimpleNamespace(allow_pg_panel_access=False, share_pg_panel_url=False, pg_password_rotation_enc="x")
        apply_access_before_save(profile, target=True, share_panel_url=True)
        self.assertTrue(profile.allow_pg_panel_access)
        self.assertIsNone(profile.pg_password_rotation_enc)
        self.assertTrue(profile.share_pg_panel_url)


class FakeOwnerPanel:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.passwords: list[str] = []
        self.modify_admin = AsyncMock(side_effect=self._modify)

    async def _modify(self, username: str, payload: dict) -> dict:
        self.passwords.append(payload["password"])
        if self.failures:
            self.failures -= 1
            raise PasarGuardError("timeout", None)
        return {"username": username}


class DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'a.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=5, telegram_id=5, referral_code="r5", role="reseller"),
                BotUser(id=6, telegram_id=6, referral_code="r6", role="reseller"),
            ])
            session.add_all([
                ResellerProfile(
                    id=1, user_id=5, pg_admin_username="shop_a", web_username="shop_a",
                    pg_admin_password_enc=encrypt_secret("Known-Old-1"), allow_pg_panel_access=True,
                ),
                ResellerProfile(
                    id=2, user_id=6, pg_admin_username="shop_b", web_username="shop_b",
                    pg_admin_password_enc=encrypt_secret("Other-Shop-1"), allow_pg_panel_access=True,
                ),
            ])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def profile(self, session, pid: int = 1) -> ResellerProfile:
        return await session.get(ResellerProfile, pid)


class RotationTests(DbCase):
    async def test_closing_access_replaces_the_known_panel_password(self) -> None:
        panel = FakeOwnerPanel()
        async with self.Session() as session:
            with patch("app.services.pg_panel_access.get_pg", return_value=panel):
                change = await set_pg_panel_access(session, await self.profile(session), False)
        self.assertEqual(change.rotation, RotationOutcome.DONE)
        async with self.Session() as session:
            row = await self.profile(session)
            other = await self.profile(session, 2)
        stored = decrypt_secret(row.pg_admin_password_enc)
        self.assertEqual(panel.passwords, [stored])
        self.assertNotEqual(stored, "Known-Old-1")
        self.assertFalse(row.allow_pg_panel_access)
        self.assertIsNone(row.pg_password_rotation_enc)
        # The other shop is untouched.
        self.assertEqual(decrypt_secret(other.pg_admin_password_enc), "Other-Shop-1")
        self.assertTrue(other.allow_pg_panel_access)

    async def test_unknown_outcome_keeps_the_saved_password_and_retry_resends_it(self) -> None:
        panel = FakeOwnerPanel(failures=1)
        async with self.Session() as session:
            with patch("app.services.pg_panel_access.get_pg", return_value=panel):
                change = await set_pg_panel_access(session, await self.profile(session), False)
                self.assertEqual(change.rotation, RotationOutcome.PENDING)
                row = await self.profile(session)
                self.assertEqual(decrypt_secret(row.pg_admin_password_enc), "Known-Old-1")
                self.assertTrue(row.pg_password_rotation_enc)
                self.assertFalse(row.allow_pg_panel_access)
                outcome = await finish_pg_password_rotation(session, row)
        self.assertEqual(outcome, RotationOutcome.DONE)
        self.assertEqual(len(panel.passwords), 2)
        self.assertEqual(panel.passwords[0], panel.passwords[1])
        async with self.Session() as session:
            row = await self.profile(session)
        self.assertEqual(decrypt_secret(row.pg_admin_password_enc), panel.passwords[0])

    async def test_two_concurrent_closes_send_one_password(self) -> None:
        panel = FakeOwnerPanel()
        with patch("app.services.pg_panel_access.get_pg", return_value=panel):
            async def close() -> None:
                async with self.Session() as session:
                    await set_pg_panel_access(session, await self.profile(session), False)

            await asyncio.gather(close(), close())
        self.assertTrue(panel.passwords)
        self.assertEqual(len(set(panel.passwords)), 1)
        async with self.Session() as session:
            row = await self.profile(session)
        self.assertEqual(decrypt_secret(row.pg_admin_password_enc), panel.passwords[0])

    async def test_reopening_does_not_touch_the_panel(self) -> None:
        panel = FakeOwnerPanel()
        async with self.Session() as session:
            row = await self.profile(session)
            row.allow_pg_panel_access = False
            await session.commit()
            with patch("app.services.pg_panel_access.get_pg", return_value=panel):
                change = await set_pg_panel_access(session, row, True)
        self.assertEqual(change.rotation, RotationOutcome.NOT_NEEDED)
        panel.modify_admin.assert_not_awaited()

    async def test_new_web_password_does_not_reach_the_panel_while_closed(self) -> None:
        panel = FakeOwnerPanel()
        async with self.Session() as session:
            row = await self.profile(session)
            row.allow_pg_panel_access = False
            with patch("app.services.pasarguard.get_pg", return_value=panel), \
                    patch("app.services.pasarguard.get_pg_for_reseller", AsyncMock(return_value=panel)):
                await apply_reseller_panel_password(session, row, "Brand-New-Pass-77", sync_pg=True)
            await session.commit()
            row = await self.profile(session)
        panel.modify_admin.assert_not_awaited()
        self.assertEqual(decrypt_secret(row.pg_admin_password_enc), "Known-Old-1")
        self.assertTrue(row.web_password_hash)


class ApiKeyWarningTests(DbCase):
    async def close(self, pid: int, *, failures: int = 0):
        async with self.Session() as session:
            profile = await self.profile(session, pid)
            with patch("app.services.pg_panel_access.get_pg", return_value=FakeOwnerPanel(failures=failures)):
                change = await set_pg_panel_access(session, profile, False)
            return await close_access_warning(session, profile, change)

    async def test_closing_a_shop_that_uses_an_api_key_tells_the_owner_to_revoke_it(self) -> None:
        async with self.Session() as session:
            (await self.profile(session)).pg_api_key_enc = encrypt_secret("key-1")
            await session.commit()
        self.assertEqual(await self.close(1), API_KEY_STILL_VALID_MESSAGE)
        self.assertIsNone(await self.close(2))  # password-only shop: the rotation is enough

    async def test_a_pending_rotation_is_reported_first(self) -> None:
        self.assertEqual(await self.close(2, failures=1), ROTATION_FAILED_MESSAGE)


class PrincipalSessionTests(DbCase):
    async def test_principal_linked_to_a_closed_shop_loses_pasarguard_capabilities(self) -> None:
        async with self.Session() as session:
            row = await self.profile(session)
            row.allow_pg_panel_access = False
            row.is_active = True
            await session.commit()
            principal = OrgPrincipal(id=40, depth=1, reseller_profile_id=1, status="active")
            staff = await _attach_linked_shop_package(session, _reseller_staff(), principal)
        self.assertEqual(staff["pg_permissions"], [])
        self.assertTrue(is_pg_panel_blocked(staff))

    async def test_principal_linked_to_an_open_shop_keeps_them(self) -> None:
        async with self.Session() as session:
            row = await self.profile(session)
            row.is_active = True
            await session.commit()
            principal = OrgPrincipal(id=41, depth=1, reseller_profile_id=1, status="active")
            staff = await _attach_linked_shop_package(session, _reseller_staff(), principal)
        self.assertIn("pg_users", staff["pg_permissions"])


class ProvisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'p.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add(BotUser(id=5, telegram_id=5, referral_code="r5", role="user"))
            session.add(ResellerPlan(
                id=1, name="closed", price=0, plan_kind="subscription", billing_mode="fixed",
                create_pg_admin=True, share_pg_panel_url=True, allow_pg_panel_access=False,
            ))
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def test_closed_plan_creates_a_private_panel_password(self) -> None:
        pg = SimpleNamespace(create_admin=AsyncMock(return_value={"id": 2}))
        async with self.Session() as session:
            user = await session.get(BotUser, 5)
            plan = await session.get(ResellerPlan, 1)
            with patch("app.services.pasarguard.get_pg", return_value=pg):
                creds = await provision_reseller(session, user=user, plan=plan, panel_base_url="https://panel")
            profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == 5))
        sent = pg.create_admin.await_args.args[0]["password"]
        self.assertNotEqual(sent, creds["web_password"])
        self.assertIsNone(creds["pg_password"])
        self.assertFalse(creds["unified_credentials"])
        self.assertFalse(creds["share_pg_panel_url"])
        self.assertEqual(creds["pg_panel_url"], "")
        self.assertNotIn(sent, format_credentials_message(creds))
        self.assertFalse(profile.allow_pg_panel_access)
        self.assertFalse(profile.share_pg_panel_url)
        self.assertEqual(decrypt_secret(profile.pg_admin_password_enc), sent)


def _load_migration():
    path = ROOT / "alembic/versions/0096_pg_panel_access.py"
    spec = importlib.util.spec_from_file_location("pg_panel_access_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.migration = _load_migration()
        self.engine = sa.create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE reseller_plans (id INTEGER PRIMARY KEY, name TEXT)"))
            conn.execute(sa.text("CREATE TABLE reseller_profiles (id INTEGER PRIMARY KEY, user_id INTEGER)"))
            conn.execute(sa.text("INSERT INTO reseller_plans (id, name) VALUES (1, 'p')"))
            conn.execute(sa.text("INSERT INTO reseller_profiles (id, user_id) VALUES (1, 9)"))

    def _run(self, conn, *, down: bool = False) -> None:
        with Operations.context(MigrationContext.configure(conn)):
            (self.migration.downgrade if down else self.migration.upgrade)()

    def _cols(self, conn, table: str) -> set[str]:
        return {c["name"] for c in sa.inspect(conn).get_columns(table)}

    def test_existing_shops_keep_access_and_the_migration_is_reversible(self) -> None:
        with self.engine.begin() as conn:
            self._run(conn)
            self._run(conn)
            self.assertEqual(
                conn.execute(sa.text("SELECT allow_pg_panel_access FROM reseller_profiles WHERE id=1")).scalar(),
                1,
            )
            self.assertEqual(
                conn.execute(sa.text("SELECT allow_pg_panel_access FROM reseller_plans WHERE id=1")).scalar(),
                1,
            )
            self.assertIn("pg_password_rotation_enc", self._cols(conn, "reseller_profiles"))
            self._run(conn, down=True)
            self.assertNotIn("allow_pg_panel_access", self._cols(conn, "reseller_profiles"))
            self.assertNotIn("pg_password_rotation_enc", self._cols(conn, "reseller_profiles"))
            self.assertNotIn("allow_pg_panel_access", self._cols(conn, "reseller_plans"))
            self.assertEqual(conn.execute(sa.text("SELECT user_id FROM reseller_profiles")).scalar(), 9)


if __name__ == "__main__":
    unittest.main()
