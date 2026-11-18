"""Phase 2B — Level-1 OrgPrincipal Web identity / login tests."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import OrgPrincipal
from app.services.authz import (
    authz_from_staff,
    has_org_global_scope,
    is_explicit_org_owner,
)
from app.services.org_principals import (
    attach_org_principal_fields,
    create_principal,
    ensure_owner_principal,
    is_owner_principal,
)
from app.services.org_scope import visible_principal_ids
from app.services.platform_identity import is_explicit_owner_staff
from app.services.principal_web_identity import (
    PrincipalWebIdentityError,
    attach_level1_web_identity,
    authenticate_level1_web,
    build_principal_session_payload,
    resolve_principal_web_session,
    session_contains_plaintext_secret,
    staff_uses_owner_pg_credentials,
)
from tests.postgres_support import scoped_async_engine


_WEB_PASSWORD = "WebLogin12!@Ab"


class Phase2BWebIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _level1(
        self, session: AsyncSession, *, pg_username: str
    ) -> OrgPrincipal:
        owner = await ensure_owner_principal(session)
        p = await create_principal(
            session,
            parent_id=int(owner.id),
            depth=1,
            pg_username=pg_username,
        )
        await session.flush()
        return p

    async def test_a_b_login_resolves_to_correct_principal(self) -> None:
        async with self.Session() as session:
            a = await self._level1(session, pg_username="pg_a")
            b = await self._level1(session, pg_username="pg_b")
            await attach_level1_web_identity(
                session, principal_id=a.id, web_username="pg_a", password=_WEB_PASSWORD
            )
            await attach_level1_web_identity(
                session, principal_id=b.id, web_username="pg_b", password=_WEB_PASSWORD
            )
            await session.commit()

            auth_a = await authenticate_level1_web(
                session, username="pg_a", password=_WEB_PASSWORD
            )
            auth_b = await authenticate_level1_web(
                session, username="pg_b", password=_WEB_PASSWORD
            )
            self.assertIsNotNone(auth_a)
            self.assertIsNotNone(auth_b)
            assert auth_a and auth_b
            self.assertEqual(int(auth_a.principal.id), int(a.id))
            self.assertEqual(int(auth_b.principal.id), int(b.id))
            self.assertNotEqual(int(auth_a.principal.id), int(auth_b.principal.id))

            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                staff_a = await resolve_principal_web_session(
                    session, build_principal_session_payload(auth_a)
                )
                staff_b = await resolve_principal_web_session(
                    session, build_principal_session_payload(auth_b)
                )
            self.assertEqual(int(staff_a["org_principal_id"]), int(a.id))
            self.assertEqual(int(staff_b["org_principal_id"]), int(b.id))

    async def test_web_username_must_match_pg(self) -> None:
        async with self.Session() as session:
            a = await self._level1(session, pg_username="pg_match")
            with self.assertRaises(PrincipalWebIdentityError) as ctx:
                await attach_level1_web_identity(
                    session,
                    principal_id=a.id,
                    web_username="other_login",
                    password=_WEB_PASSWORD,
                )
            self.assertEqual(ctx.exception.code, "web_pg_mismatch")

    async def test_c_a_cannot_become_owner(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            a = await self._level1(session, pg_username="pg_c")
            await attach_level1_web_identity(
                session, principal_id=a.id, web_username="pg_c", password=_WEB_PASSWORD
            )
            await session.commit()
            auth = await authenticate_level1_web(
                session, username="pg_c", password=_WEB_PASSWORD
            )
            assert auth
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                staff = await resolve_principal_web_session(
                    session, build_principal_session_payload(auth)
                )
            self.assertFalse(is_owner_principal(auth.principal))
            self.assertFalse(is_explicit_owner_staff(staff))
            self.assertFalse(is_explicit_org_owner(authz_from_staff(staff)))
            self.assertFalse(has_org_global_scope(authz_from_staff(staff)))
            self.assertNotEqual(int(staff["org_principal_id"]), int(owner.id))
            self.assertFalse(bool(staff.get("web_owner")))

    async def test_d_a_cannot_become_b(self) -> None:
        async with self.Session() as session:
            a = await self._level1(session, pg_username="pg_d_a")
            b = await self._level1(session, pg_username="pg_d_b")
            await attach_level1_web_identity(
                session, principal_id=a.id, web_username="pg_d_a", password=_WEB_PASSWORD
            )
            await attach_level1_web_identity(
                session, principal_id=b.id, web_username="pg_d_b", password=_WEB_PASSWORD
            )
            await session.commit()
            auth_a = await authenticate_level1_web(
                session, username="pg_d_a", password=_WEB_PASSWORD
            )
            assert auth_a
            cookie = build_principal_session_payload(auth_a)
            # Attempt to spoof B's principal id in the cookie
            cookie["org_principal_id"] = int(b.id)
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                with self.assertRaises(PrincipalWebIdentityError) as ctx:
                    await resolve_principal_web_session(session, cookie)
            self.assertEqual(ctx.exception.code, "principal_tamper")

    async def test_e_disabled_principal_deny(self) -> None:
        async with self.Session() as session:
            a = await self._level1(session, pg_username="pg_e")
            await attach_level1_web_identity(
                session, principal_id=a.id, web_username="pg_e", password=_WEB_PASSWORD
            )
            a.status = "disabled"
            await session.commit()
            with self.assertRaises(PrincipalWebIdentityError) as ctx:
                await authenticate_level1_web(
                    session, username="pg_e", password=_WEB_PASSWORD
                )
            self.assertEqual(ctx.exception.code, "principal_disabled")

    async def test_e2_disabled_identity_deny(self) -> None:
        async with self.Session() as session:
            a = await self._level1(session, pg_username="pg_e2")
            ident = await attach_level1_web_identity(
                session, principal_id=a.id, web_username="pg_e2", password=_WEB_PASSWORD
            )
            ident.is_active = False
            await session.commit()
            with self.assertRaises(PrincipalWebIdentityError) as ctx:
                await authenticate_level1_web(
                    session, username="pg_e2", password=_WEB_PASSWORD
                )
            self.assertEqual(ctx.exception.code, "identity_disabled")

    async def test_f_missing_mapping_deny(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            await session.commit()
            with self.assertRaises(PrincipalWebIdentityError) as ctx:
                await resolve_principal_web_session(
                    session,
                    {
                        "role": "principal",
                        "username": "ghost",
                        "web_identity_id": 0,
                        "pv": "x",
                    },
                )
            self.assertEqual(ctx.exception.code, "missing_mapping")
            with self.assertRaises(PrincipalWebIdentityError) as ctx2:
                await resolve_principal_web_session(
                    session,
                    {
                        "role": "principal",
                        "username": "ghost",
                        "web_identity_id": 99999,
                        "pv": "x",
                    },
                )
            self.assertEqual(ctx2.exception.code, "missing_mapping")

    async def test_g_principal_id_tamper_deny(self) -> None:
        async with self.Session() as session:
            a = await self._level1(session, pg_username="pg_g")
            b = await self._level1(session, pg_username="pg_g_b")
            await attach_level1_web_identity(
                session, principal_id=a.id, web_username="pg_g", password=_WEB_PASSWORD
            )
            await session.commit()
            auth = await authenticate_level1_web(
                session, username="pg_g", password=_WEB_PASSWORD
            )
            assert auth
            cookie = build_principal_session_payload(auth)
            cookie["org_principal_id"] = int(b.id)
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                with self.assertRaises(PrincipalWebIdentityError) as ctx:
                    await resolve_principal_web_session(session, cookie)
            self.assertEqual(ctx.exception.code, "principal_tamper")

    async def test_h_depth_parent_tamper_deny(self) -> None:
        async with self.Session() as session:
            a = await self._level1(session, pg_username="pg_h")
            await attach_level1_web_identity(
                session, principal_id=a.id, web_username="pg_h", password=_WEB_PASSWORD
            )
            await session.commit()
            auth = await authenticate_level1_web(
                session, username="pg_h", password=_WEB_PASSWORD
            )
            assert auth
            cookie = build_principal_session_payload(auth)
            cookie["org_depth"] = 0
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                with self.assertRaises(PrincipalWebIdentityError) as ctx:
                    await resolve_principal_web_session(session, cookie)
            self.assertEqual(ctx.exception.code, "hierarchy_tamper")

            cookie2 = build_principal_session_payload(auth)
            cookie2["org_parent_id"] = 0
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                with self.assertRaises(PrincipalWebIdentityError) as ctx2:
                    await resolve_principal_web_session(session, cookie2)
            self.assertEqual(ctx2.exception.code, "hierarchy_tamper")

    async def test_i_role_admin_alone_not_owner(self) -> None:
        staff = {"role": "admin", "username": "legacy"}
        self.assertFalse(is_explicit_owner_staff(staff))
        self.assertFalse(is_explicit_org_owner(authz_from_staff(staff)))
        self.assertFalse(has_org_global_scope(authz_from_staff(staff)))

    async def test_j_scope_isolated_to_self(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            a = await self._level1(session, pg_username="pg_j_a")
            b = await self._level1(session, pg_username="pg_j_b")
            await attach_level1_web_identity(
                session, principal_id=a.id, web_username="pg_j_a", password=_WEB_PASSWORD
            )
            await session.commit()
            auth = await authenticate_level1_web(
                session, username="pg_j_a", password=_WEB_PASSWORD
            )
            assert auth
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                staff = await resolve_principal_web_session(
                    session, build_principal_session_payload(auth)
                )
            visible = frozenset(int(x) for x in staff["org_visible_principal_ids"])
            self.assertEqual(visible, frozenset({int(a.id)}))
            self.assertNotIn(int(owner.id), visible)
            self.assertNotIn(int(b.id), visible)
            live = await visible_principal_ids(session, a)
            self.assertEqual(live, frozenset({int(a.id)}))

    async def test_k_no_owner_pg_credentials(self) -> None:
        async with self.Session() as session:
            a = await self._level1(session, pg_username="pg_own_creds")
            await attach_level1_web_identity(
                session, principal_id=a.id, web_username="pg_own_creds", password=_WEB_PASSWORD
            )
            await session.commit()
            auth = await authenticate_level1_web(
                session, username="pg_own_creds", password=_WEB_PASSWORD
            )
            assert auth
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ), patch(
                "app.services.principal_web_identity.owner_env_pg_username",
                return_value="env_owner_pg",
            ):
                staff = await resolve_principal_web_session(
                    session, build_principal_session_payload(auth)
                )
                self.assertEqual(staff.get("pg_admin_username"), "pg_own_creds")
                self.assertFalse(staff_uses_owner_pg_credentials(staff))

    async def test_l_owner_login_path_unchanged(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            await session.commit()
            staff = attach_org_principal_fields(
                {"role": "admin", "web_owner": True, "username": "owner"},
                owner,
                visible_principal_ids=await visible_principal_ids(session, owner),
            )
            self.assertTrue(is_explicit_owner_staff(staff))
            self.assertTrue(is_explicit_org_owner(authz_from_staff(staff)))
            self.assertTrue(has_org_global_scope(authz_from_staff(staff)))
            # Owner session still uses role=admin + web_owner, not role=principal
            self.assertEqual(staff.get("role"), "admin")
            self.assertTrue(staff.get("web_owner"))

    async def test_n_session_no_plaintext_pg_password(self) -> None:
        async with self.Session() as session:
            a = await self._level1(session, pg_username="pg_n")
            await attach_level1_web_identity(
                session, principal_id=a.id, web_username="pg_n", password=_WEB_PASSWORD
            )
            await session.commit()
            auth = await authenticate_level1_web(
                session, username="pg_n", password=_WEB_PASSWORD
            )
            assert auth
            payload = build_principal_session_payload(auth)
            self.assertFalse(session_contains_plaintext_secret(payload))
            self.assertNotIn("password", payload)
            self.assertNotIn("pg_password", payload)
            self.assertNotIn("web_password", payload)
            # pv is hash prefix only
            self.assertTrue(str(payload.get("pv") or "").startswith("$2"))


if __name__ == "__main__":
    unittest.main()
