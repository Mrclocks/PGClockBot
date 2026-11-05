"""Targeted authz invariants: L1/L2 must not consume Owner PG credentials."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import OrgPrincipal
from app.services.org_principals import (
    OrgPrincipalError,
    create_principal,
    ensure_owner_principal,
)
from app.services.pasarguard import PasarGuardError, get_pg_for_principal
from app.services.pg_access import (
    clear_platform_pg_capability_cache,
    enrich_staff_pg_from_role,
    resolve_reseller_pg_features,
    staff_has_pg_admins_create,
)
from app.services.pg_staff_access import resolve_pg_role_id_for_admin
from app.services.principal_child_provisioning import has_pg_admin_create_capability
from app.services.principal_lifecycle import _safe_pg_role_name
from app.services.principal_provisioning import owner_has_pg_admin_create_capability
from app.services.secret_box import encrypt_secret


_PG_A = "SiblingA12!@xx"
_PG_B = "SiblingB34!@yy"


class PrincipalPgAuthzHardenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine(
            "sqlite+aiosqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        clear_platform_pg_capability_cache()

    async def asyncTearDown(self) -> None:
        clear_platform_pg_capability_cache()
        await self.engine.dispose()

    async def _siblings(self, session: AsyncSession) -> tuple[OrgPrincipal, OrgPrincipal]:
        owner = await ensure_owner_principal(session)
        a = await create_principal(
            session,
            parent_id=int(owner.id),
            depth=1,
            pg_username="l1_a",
            pg_password_enc=encrypt_secret(_PG_A),
        )
        b = await create_principal(
            session,
            parent_id=int(owner.id),
            depth=1,
            pg_username="l1_b",
            pg_password_enc=encrypt_secret(_PG_B),
        )
        await session.commit()
        return a, b

    async def test_a_l1_role_lookup_does_not_call_owner_get_pg(self) -> None:
        client = AsyncMock()
        client.get_admin_role = AsyncMock(
            return_value={"id": 80, "name": "Op", "is_owner": False, "permissions": {}}
        )
        client.get_admin = AsyncMock(
            return_value={"username": "l1_a", "role": {"id": 80}}
        )
        client.get_admin_roles = AsyncMock(return_value=[{"id": 80, "name": "Op"}])
        with patch(
            "app.services.pasarguard.get_pg",
            side_effect=AssertionError("L1/L2 must not use Owner get_pg()"),
        ):
            features, role = await resolve_reseller_pg_features(80, client=client)
            live = await resolve_pg_role_id_for_admin("l1_a", client=client)
            name = await _safe_pg_role_name(80, client=client)
        self.assertEqual(live, 80)
        self.assertEqual(name, "Op")
        self.assertIsInstance(features, list)
        self.assertIsInstance(role, dict)
        client.get_admin_role.assert_awaited()
        client.get_admin.assert_awaited()

        with patch(
            "app.services.pasarguard.get_pg",
            side_effect=AssertionError("implicit Owner get_pg()"),
        ):
            self.assertIsNone(await _safe_pg_role_name(80))
            from app.api.principal_pages import _chips_for_role_id

            self.assertEqual(await _chips_for_role_id(80, client=None), [])

    async def test_b_sibling_cannot_resolve_other_principal_client(self) -> None:
        from app.services import pasarguard as pg_mod

        async with self.Session() as session:
            a, b = await self._siblings(session)
            pg_mod._pg_principal_cache.clear()

            class _Client:
                def __init__(self, *, username=None, password=None, access_token=None):
                    self.username = username
                    self._token = "t"
                    self._login_password = password

                async def ensure_token(self):
                    self._login_password = None
                    return self._token

            with patch.object(pg_mod, "PasarGuardClient", _Client):
                ca = await get_pg_for_principal(session, principal_id=int(a.id))
                cb = await get_pg_for_principal(session, principal_id=int(b.id))
                swapped = await get_pg_for_principal(
                    session,
                    principal_id=int(a.id),
                    pg_username=b.pg_username,
                )
            self.assertEqual(ca.username, "l1_a")
            self.assertEqual(cb.username, "l1_b")
            self.assertEqual(swapped.username, "l1_a")
            self.assertNotEqual(ca.username, cb.username)

    async def test_c_owner_singleton_under_concurrent_create(self) -> None:
        async with self.engine.connect() as conn:
            names = await conn.run_sync(
                lambda sync_conn: {
                    ix["name"]
                    for ix in inspect(sync_conn).get_indexes("org_principals")
                }
            )
            self.assertIn("uq_org_principals_single_owner", names)

        async with self.Session() as session:
            first = await ensure_owner_principal(session)
            await session.commit()
            first_id = int(first.id)
        async with self.Session() as session:
            again = await ensure_owner_principal(session)
            self.assertEqual(int(again.id), first_id)

        async with self.Session() as session:
            with self.assertRaises(OrgPrincipalError):
                await create_principal(session, parent_id=None, depth=0)
            owners = list(
                (
                    await session.execute(
                        select(OrgPrincipal).where(OrgPrincipal.depth == 0)
                    )
                )
                .scalars()
                .all()
            )
            self.assertEqual(len(owners), 1)
            with self.assertRaises(IntegrityError):
                await session.execute(
                    text(
                        "INSERT INTO org_principals (depth, parent_id, status) "
                        "VALUES (0, NULL, 'disabled')"
                    )
                )
                await session.commit()
            await session.rollback()

    async def test_d_username_alone_does_not_select_principal(self) -> None:
        async with self.Session() as session:
            a, _b = await self._siblings(session)
            with self.assertRaises(PasarGuardError):
                await get_pg_for_principal(session, pg_username=a.pg_username)
            with self.assertRaises(PasarGuardError):
                await get_pg_for_principal(session, principal_id=None, pg_username="l1_a")
            with self.assertRaises(PasarGuardError):
                await get_pg_for_principal(session)

    def test_e_page_visibility_without_admins_create_denied(self) -> None:
        page_only = {
            "pg_is_owner": False,
            "pg_permissions": ["pg_admins", "pg_users"],
            "pg_actions": {"users": {"create": True}},
            "pg_role": {
                "is_owner": False,
                "permissions": {"users": {"create": True}},
            },
        }
        self.assertFalse(staff_has_pg_admins_create(page_only))
        self.assertFalse(has_pg_admin_create_capability(page_only))
        self.assertFalse(owner_has_pg_admin_create_capability(page_only))

        real = enrich_staff_pg_from_role(
            {"pg_is_owner": False, "pg_permissions": []},
            [],
            {
                "id": 4,
                "name": "Delegated",
                "is_owner": False,
                "permissions": {"admins": {"create": True}},
            },
        )
        self.assertTrue(staff_has_pg_admins_create(real))
        self.assertTrue(has_pg_admin_create_capability(real))

    def test_f_owner_explicit_capability_still_works(self) -> None:
        owner = {"pg_is_owner": True, "pg_permissions": []}
        self.assertTrue(owner_has_pg_admin_create_capability(owner))
        self.assertFalse(has_pg_admin_create_capability(owner))

        owner_via_action = {
            "pg_is_owner": False,
            "pg_actions": {"admins": {"create": True}},
        }
        self.assertTrue(owner_has_pg_admin_create_capability(owner_via_action))


class ResellerPlansStaffCtxTests(unittest.IsolatedAsyncioTestCase):
    async def test_staff_ctx_uses_reseller_client_not_owner_get_pg(self) -> None:
        from app.bot.handlers.reseller_plans import _staff_ctx

        profile = MagicMock()
        profile.user_id = 9
        profile.pg_role_id = 12
        client = AsyncMock()
        client.get_admin_role = AsyncMock(
            return_value={"id": 12, "name": "R", "is_owner": False, "permissions": {}}
        )
        with (
            patch(
                "app.services.pasarguard.get_pg_for_reseller",
                new=AsyncMock(return_value=client),
            ),
            patch(
                "app.services.pasarguard.get_pg",
                side_effect=AssertionError("reseller L1 must not use Owner get_pg()"),
            ),
        ):
            clear_platform_pg_capability_cache()
            staff = await _staff_ctx(profile, MagicMock())
        self.assertEqual(staff["role"], "reseller")
        self.assertEqual(staff["bot_user_id"], 9)
        client.get_admin_role.assert_awaited()
