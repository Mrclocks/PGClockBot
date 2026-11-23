"""Phase 3C — Level-2 Principal Web identity tests.

Reuses Phase 2B login / session / resolve. No second auth system.
Never assert or print actual credential values beyond inequality checks.
"""

from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import OrgPrincipal
from app.services.authz import (
    authz_from_staff,
    authorize,
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
from app.services.pasarguard import PasarGuardError, get_pg_for_principal
from app.services.platform_identity import is_explicit_owner_staff
from app.services.principal_pg_authz import authorize_pg_page
from app.services.principal_web_identity import (
    PrincipalWebIdentityError,
    attach_level1_web_identity,
    attach_level2_web_identity,
    authenticate_level1_web,
    build_principal_session_payload,
    resolve_principal_web_session,
    session_contains_plaintext_secret,
    staff_uses_owner_pg_credentials,
)
from app.services.resource_principal import (
    resolve_resource_principal,
    resource_in_principal_scope,
)
from app.services.secret_box import encrypt_secret
from tests.postgres_support import scoped_async_engine


_WEB_A1 = "WebA1login12!@"
_WEB_A2 = "WebA2login12!@"
_PG_A = "ParentA12!@xx"
_PG_A1 = "ChildA112!@yy"
_PG_A2 = "ChildA234!@zz"
_PG_B = "ParentB56!@aa"
_PG_B1 = "ChildB178!@bb"
_OWNER_PG = "env_owner_pg"


class CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(self.format(record))
        except Exception:
            self.messages.append(record.getMessage())


class Phase3CLevel2WebIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _tree(self, session: AsyncSession):
        owner = await ensure_owner_principal(session)
        a = await create_principal(
            session,
            parent_id=int(owner.id),
            depth=1,
            pg_username="l1_a",
            pg_password_enc=encrypt_secret(_PG_A),
        )
        a1 = await create_principal(
            session,
            parent_id=int(a.id),
            depth=2,
            pg_username="l2_a1",
            pg_password_enc=encrypt_secret(_PG_A1),
        )
        a2 = await create_principal(
            session,
            parent_id=int(a.id),
            depth=2,
            pg_username="l2_a2",
            pg_password_enc=encrypt_secret(_PG_A2),
        )
        b = await create_principal(
            session,
            parent_id=int(owner.id),
            depth=1,
            pg_username="l1_b",
            pg_password_enc=encrypt_secret(_PG_B),
        )
        b1 = await create_principal(
            session,
            parent_id=int(b.id),
            depth=2,
            pg_username="l2_b1",
            pg_password_enc=encrypt_secret(_PG_B1),
        )
        await attach_level1_web_identity(
            session, principal_id=int(a.id), web_username="l1_a", password=_WEB_A1
        )
        await attach_level2_web_identity(
            session, principal_id=int(a1.id), web_username="l2_a1", password=_WEB_A1
        )
        await attach_level2_web_identity(
            session, principal_id=int(a2.id), web_username="l2_a2", password=_WEB_A2
        )
        await session.commit()
        return owner, a, a1, a2, b, b1

    async def _resolve(self, session, auth):
        with patch(
            "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
            new=AsyncMock(return_value=None),
        ), patch(
            "app.services.principal_web_identity.owner_env_pg_username",
            return_value=_OWNER_PG,
        ):
            return await resolve_principal_web_session(
                session, build_principal_session_payload(auth)
            )

    async def test_a_a1_login_is_a1(self) -> None:
        async with self.Session() as session:
            _o, _a, a1, _a2, _b, _b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB_A1
            )
            self.assertIsNotNone(auth)
            assert auth is not None
            self.assertEqual(int(auth.principal.id), int(a1.id))
            self.assertEqual(int(auth.principal.depth), 2)
            staff = await self._resolve(session, auth)
            self.assertEqual(int(staff["org_principal_id"]), int(a1.id))
            self.assertEqual(int(staff["org_depth"]), 2)

    async def test_b_a2_login_is_a2(self) -> None:
        async with self.Session() as session:
            _o, _a, _a1, a2, _b, _b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a2", password=_WEB_A2
            )
            assert auth is not None
            self.assertEqual(int(auth.principal.id), int(a2.id))
            staff = await self._resolve(session, auth)
            self.assertEqual(int(staff["org_principal_id"]), int(a2.id))

    async def test_c_a1_cannot_become_a(self) -> None:
        async with self.Session() as session:
            _o, a, a1, _a2, _b, _b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB_A1
            )
            assert auth is not None
            cookie = build_principal_session_payload(auth)
            cookie["org_principal_id"] = int(a.id)
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                with self.assertRaises(PrincipalWebIdentityError) as ctx:
                    await resolve_principal_web_session(session, cookie)
            self.assertEqual(ctx.exception.code, "principal_tamper")
            staff = await self._resolve(session, auth)
            self.assertNotEqual(int(staff["org_principal_id"]), int(a.id))
            self.assertEqual(int(staff["org_principal_id"]), int(a1.id))

    async def test_d_a1_cannot_become_a2(self) -> None:
        async with self.Session() as session:
            _o, _a, a1, a2, _b, _b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB_A1
            )
            assert auth is not None
            cookie = build_principal_session_payload(auth)
            cookie["org_principal_id"] = int(a2.id)
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                with self.assertRaises(PrincipalWebIdentityError) as ctx:
                    await resolve_principal_web_session(session, cookie)
            self.assertEqual(ctx.exception.code, "principal_tamper")
            self.assertEqual(int(auth.principal.id), int(a1.id))

    async def test_e_a1_cannot_become_owner(self) -> None:
        async with self.Session() as session:
            owner, _a, a1, _a2, _b, _b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB_A1
            )
            assert auth is not None
            staff = await self._resolve(session, auth)
            self.assertFalse(is_owner_principal(auth.principal))
            self.assertFalse(is_explicit_owner_staff(staff))
            self.assertFalse(is_explicit_org_owner(authz_from_staff(staff)))
            self.assertFalse(has_org_global_scope(authz_from_staff(staff)))
            self.assertNotEqual(int(staff["org_principal_id"]), int(owner.id))
            self.assertFalse(bool(staff.get("web_owner")))
            cookie = build_principal_session_payload(auth)
            cookie["org_principal_id"] = int(owner.id)
            cookie["org_depth"] = 0
            cookie["web_owner"] = True
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                with self.assertRaises(PrincipalWebIdentityError):
                    await resolve_principal_web_session(session, cookie)
            _ = a1

    async def test_f_a1_scope_self_only(self) -> None:
        async with self.Session() as session:
            owner, a, a1, a2, b, b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB_A1
            )
            assert auth is not None
            staff = await self._resolve(session, auth)
            visible = frozenset(int(x) for x in staff["org_visible_principal_ids"])
            self.assertEqual(visible, frozenset({int(a1.id)}))
            live = await visible_principal_ids(session, a1)
            self.assertEqual(live, frozenset({int(a1.id)}))
            parent_live = await visible_principal_ids(session, a)
            self.assertEqual(parent_live, frozenset({int(a.id), int(a1.id), int(a2.id)}))
            owner_live = await visible_principal_ids(session, owner)
            self.assertTrue({int(a.id), int(a1.id), int(b.id), int(b1.id)} <= owner_live)

    def _resource_ok(self, staff: dict, resource_pid: int) -> bool:
        res = resolve_resource_principal(
            SimpleNamespace(owner_principal_id=int(resource_pid))
        )
        vis = frozenset(int(x) for x in staff.get("org_visible_principal_ids") or [])
        scoped = resource_in_principal_scope(
            actor_visible_principal_ids=vis, resolution=res
        )
        decision = authorize(
            authenticated=True,
            ctx=authz_from_staff(staff),
            resource_principal_id=int(resource_pid),
            pg_permission_ok=True,
            local_safety_ok=True,
        )
        return scoped and decision.allowed

    async def test_g_a1_cannot_access_a_resources(self) -> None:
        async with self.Session() as session:
            _o, a, _a1, _a2, _b, _b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB_A1
            )
            assert auth is not None
            staff = await self._resolve(session, auth)
            self.assertFalse(self._resource_ok(staff, int(a.id)))

    async def test_h_a1_cannot_access_a2_resources(self) -> None:
        async with self.Session() as session:
            _o, _a, _a1, a2, _b, _b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB_A1
            )
            assert auth is not None
            staff = await self._resolve(session, auth)
            self.assertFalse(self._resource_ok(staff, int(a2.id)))

    async def test_i_a1_cannot_access_b_branch(self) -> None:
        async with self.Session() as session:
            _o, _a, _a1, _a2, b, b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB_A1
            )
            assert auth is not None
            staff = await self._resolve(session, auth)
            self.assertFalse(self._resource_ok(staff, int(b.id)))
            self.assertFalse(self._resource_ok(staff, int(b1.id)))

    async def test_j_disabled_a1_deny(self) -> None:
        async with self.Session() as session:
            _o, _a, a1, _a2, _b, _b1 = await self._tree(session)
            a1.status = "disabled"
            await session.commit()
            with self.assertRaises(PrincipalWebIdentityError) as ctx:
                await authenticate_level1_web(
                    session, username="l2_a1", password=_WEB_A1
                )
            self.assertEqual(ctx.exception.code, "principal_disabled")

    async def test_k_disabled_parent_login_deny(self) -> None:
        async with self.Session() as session:
            _o, a, _a1, _a2, _b, _b1 = await self._tree(session)
            a.status = "disabled"
            await session.commit()
            with self.assertRaises(PrincipalWebIdentityError) as ctx:
                await authenticate_level1_web(
                    session, username="l2_a1", password=_WEB_A1
                )
            self.assertEqual(ctx.exception.code, "parent_disabled")

    async def test_l_session_after_parent_disable_deny(self) -> None:
        async with self.Session() as session:
            _o, a, _a1, _a2, _b, _b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB_A1
            )
            assert auth is not None
            payload = build_principal_session_payload(auth)
            a.status = "disabled"
            await session.commit()
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                with self.assertRaises(PrincipalWebIdentityError) as ctx:
                    await resolve_principal_web_session(session, payload)
            self.assertEqual(ctx.exception.code, "parent_disabled")

    async def test_m_missing_web_identity_deny(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            a = await create_principal(session, parent_id=int(owner.id), depth=1)
            orphan = await create_principal(
                session,
                parent_id=int(a.id),
                depth=2,
                pg_username="orphan_l2",
                pg_password_enc=encrypt_secret(_PG_A1),
            )
            await session.commit()
            self.assertIsNone(
                await authenticate_level1_web(
                    session, username="no_such_web", password=_WEB_A1
                )
            )
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
            _ = orphan

    async def test_n_missing_pg_credential_deny(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            a = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="l1_npg",
                pg_password_enc=encrypt_secret(_PG_A),
            )
            child = await create_principal(
                session,
                parent_id=int(a.id),
                depth=2,
                pg_username="l2_npg",
                pg_password_enc=None,
            )
            await attach_level2_web_identity(
                session,
                principal_id=int(child.id),
                web_username="l2_npg",
                password=_WEB_A1,
            )
            await session.commit()
            with self.assertRaises(PrincipalWebIdentityError) as ctx:
                await authenticate_level1_web(
                    session, username="l2_npg", password=_WEB_A1
                )
            self.assertEqual(ctx.exception.code, "pg_credential_missing")

    async def test_o_p_a1_uses_own_pg_credentials(self) -> None:
        from app.services import pasarguard as pg_mod

        async with self.Session() as session:
            _o, a, a1, a2, _b, _b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB_A1
            )
            assert auth is not None
            staff = await self._resolve(session, auth)
            self.assertEqual(staff.get("pg_admin_username"), "l2_a1")
            self.assertNotEqual(staff.get("pg_admin_username"), a.pg_username)
            self.assertNotEqual(staff.get("pg_admin_username"), _OWNER_PG)
            self.assertFalse(staff_uses_owner_pg_credentials(staff))
            self.assertTrue(staff.get("pg_credentials_ready"))

            pg_mod._pg_principal_cache.clear()
            captured: dict[str, str | None] = {}

            class _Client:
                def __init__(self, *, username=None, password=None, access_token=None):
                    captured["username"] = username
                    captured["password"] = password
                    self._token = "t"
                    self._login_password = password

                async def ensure_token(self):
                    self._login_password = None
                    return self._token

            with patch.object(pg_mod, "PasarGuardClient", _Client):
                client = await get_pg_for_principal(
                    session, principal_id=int(a1.id)
                )
            self.assertEqual(captured.get("username"), "l2_a1")
            self.assertNotEqual(captured.get("username"), "l1_a")
            self.assertNotEqual(captured.get("username"), "l2_a2")
            self.assertNotEqual(captured.get("username"), _OWNER_PG)
            self.assertIsNone(client._login_password)
            # Cache isolated from parent / sibling
            self.assertIn((int(a1.id), "l2_a1"), pg_mod._pg_principal_cache)
            self.assertNotIn((int(a.id), "l2_a1"), pg_mod._pg_principal_cache)
            self.assertNotIn((int(a2.id), "l2_a1"), pg_mod._pg_principal_cache)

    async def test_q_cookie_tamper_denied_or_unchanged(self) -> None:
        async with self.Session() as session:
            _o, a, a1, a2, _b, _b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB_A1
            )
            assert auth is not None
            # Untampered cookie → server identity A1
            staff = await self._resolve(session, auth)
            self.assertEqual(int(staff["org_principal_id"]), int(a1.id))
            self.assertEqual(int(staff["org_depth"]), 2)
            self.assertEqual(int(staff["org_parent_id"]), int(a.id))

            cookie = build_principal_session_payload(auth)
            cookie["org_depth"] = 1
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                with self.assertRaises(PrincipalWebIdentityError) as ctx:
                    await resolve_principal_web_session(session, cookie)
            self.assertEqual(ctx.exception.code, "hierarchy_tamper")

            cookie2 = build_principal_session_payload(auth)
            cookie2["org_parent_id"] = int(a2.id)
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                with self.assertRaises(PrincipalWebIdentityError) as ctx2:
                    await resolve_principal_web_session(session, cookie2)
            self.assertEqual(ctx2.exception.code, "hierarchy_tamper")

    async def test_r_pg_role_name_not_hierarchy(self) -> None:
        async with self.Session() as session:
            _o, _a, a1, _a2, _b, _b1 = await self._tree(session)
            a1.pg_username = "Administrator"
            await session.commit()
            # Re-attach would collide; login still binds to same principal_id
            # Update identity mapping's principal still depth 2
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB_A1
            )
            assert auth is not None
            self.assertEqual(int(auth.principal.id), int(a1.id))
            self.assertEqual(int(auth.principal.depth), 2)
            self.assertFalse(is_owner_principal(auth.principal))
            staff = await self._resolve(session, auth)
            staff["pg_role"] = {"name": "Owner", "is_owner": True}
            self.assertEqual(int(staff["org_depth"]), 2)
            self.assertFalse(bool(staff.get("web_owner")))
            self.assertFalse(authorize_pg_page(staff, "pg_admins").allowed)

    async def test_t_no_plaintext_in_session_or_logs(self) -> None:
        handler = CapturingHandler()
        loggers = [
            logging.getLogger("app.services.principal_web_identity"),
            logging.getLogger("app.services.pasarguard"),
            logging.getLogger("app.services.secret_box"),
        ]
        for lg in loggers:
            lg.addHandler(handler)
            lg.setLevel(logging.DEBUG)
        try:
            async with self.Session() as session:
                _o, _a, _a1, _a2, _b, _b1 = await self._tree(session)
                auth = await authenticate_level1_web(
                    session, username="l2_a1", password=_WEB_A1
                )
                assert auth is not None
                payload = build_principal_session_payload(auth)
                self.assertFalse(session_contains_plaintext_secret(payload))
                blob = str(payload)
                self.assertNotIn(_PG_A1, blob)
                self.assertNotIn(_PG_A, blob)
                self.assertNotIn(_WEB_A1, blob)
                self.assertNotIn("pg_password", payload)
                self.assertNotIn("pg_password_enc", payload)
                staff = await self._resolve(session, auth)
                public = {k: v for k, v in staff.items() if not str(k).startswith("_")}
                pub = str(public)
                self.assertNotIn(_PG_A1, pub)
                self.assertNotIn("pg_password_enc", public)
                joined = "\n".join(handler.messages)
                self.assertNotIn(_PG_A1, joined)
                self.assertNotIn(_WEB_A1, joined)
        finally:
            for lg in loggers:
                lg.removeHandler(handler)

    async def test_attach_level1_rejects_l2(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            a = await create_principal(session, parent_id=int(owner.id), depth=1)
            child = await create_principal(session, parent_id=int(a.id), depth=2)
            await session.commit()
            with self.assertRaises(PrincipalWebIdentityError) as ctx:
                await attach_level1_web_identity(
                    session,
                    principal_id=int(child.id),
                    web_username="dup_l2",
                    password=_WEB_A1,
                )
            self.assertEqual(ctx.exception.code, "not_level1")

    async def test_disabled_parent_blocks_pg_client(self) -> None:
        async with self.Session() as session:
            _o, a, a1, _a2, _b, _b1 = await self._tree(session)
            a.status = "disabled"
            await session.commit()
            with self.assertRaises(PasarGuardError):
                await get_pg_for_principal(session, principal_id=int(a1.id))


if __name__ == "__main__":
    unittest.main()
