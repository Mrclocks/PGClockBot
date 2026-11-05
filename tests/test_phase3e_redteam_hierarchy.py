"""Phase 3E — red-team hierarchy security audit (attack tests only).

Each case expects DENY / fail-closed. Failures are confirmed bypasses.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import BotUser, Order, Ticket, UserService
from app.services.authz import (
    authz_from_staff,
    authorize,
    has_org_global_scope,
    is_explicit_org_owner,
)
from app.bot.auth import is_bot_owner_principal, is_platform_admin as bot_is_platform_admin
from app.services.org_principals import (
    OrgPrincipalError,
    attach_org_principal_fields,
    create_principal,
    ensure_owner_principal,
    is_owner_principal,
    resolve_org_principal_for_staff,
)
from app.services.org_scope import visible_principal_ids
from app.services.pasarguard import PasarGuardError, get_pg_for_principal
from app.services.pg_access import staff_pg_action
from app.services.pg_object_scope import pg_object_in_staff_scope
from app.services.pg_user_scope import pg_user_in_staff_scope
from app.services.platform_identity import is_explicit_owner_staff, is_web_platform_admin
from app.services.principal_child_provisioning import (
    ChildProvisionError,
    Level2ProvisionRequest,
    provision_level2_child,
    scoped_idempotency_key,
)
from app.services.principal_lifecycle import (
    PrincipalLifecycleError,
    disable_level1_principal,
    reject_hierarchy_mutation,
)
from app.services.principal_pg_authz import (
    apply_principal_pg_local_safety,
    authorize_pg_action,
    authorize_pg_page,
    staff_may_pg_page,
)
from app.services.principal_provisioning import (
    Level1ProvisionRequest,
    PrincipalProvisionError,
    provision_level1_principal,
)
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
from app.services.shop_scope import is_platform_admin, shop_owner_id


_WEB = "WebRed12!@Ab"
_PG_A = "ParentA12!@xx"
_PG_A1 = "ChildA112!@yy"
_PG_A2 = "ChildA234!@zz"
_PG_B = "ParentB56!@aa"
_PG_B1 = "ChildB178!@bb"

BROAD = {
    "users": {"read": True, "create": True, "update": True, "delete": True},
    "nodes": {"read": True, "update": True, "create": True, "delete": True},
    "hosts": {"read": True, "update": True, "create": True, "delete": True},
    "templates": {"read": True, "update": True, "create": True, "delete": True},
    "groups": {"read": True, "update": True, "create": True, "delete": True},
}
NARROW = {
    "users": {"read": True, "read_simple": True, "update": False, "create": False, "delete": False},
}


def _role(name: str, rid: int, perms: dict) -> dict:
    return {"id": rid, "name": name, "is_owner": False, "permissions": perms}


def _owner_staff(owner) -> dict:
    return attach_org_principal_fields(
        {
            "role": "admin",
            "web_owner": True,
            "username": "owner",
            "pg_is_owner": True,
            "pg_permissions": ["pg_admins", "pg_users"],
        },
        owner,
        visible_principal_ids=frozenset({int(owner.id)}),
    )


class Phase3ERedTeamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _tree(self, session):
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
            session, principal_id=int(a.id), web_username="l1_a", password=_WEB
        )
        await attach_level2_web_identity(
            session, principal_id=int(a1.id), web_username="l2_a1", password=_WEB
        )
        await attach_level2_web_identity(
            session, principal_id=int(a2.id), web_username="l2_a2", password=_WEB
        )
        await attach_level2_web_identity(
            session, principal_id=int(b1.id), web_username="l2_b1", password=_WEB
        )
        await session.commit()
        return owner, a, a1, a2, b, b1

    async def _resolve(self, session, auth, extra_cookie: dict | None = None):
        payload = build_principal_session_payload(auth)
        if extra_cookie:
            payload.update(extra_cookie)
        with patch(
            "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
            new=AsyncMock(return_value=None),
        ), patch(
            "app.services.principal_web_identity.owner_env_pg_username",
            return_value="env_owner_pg",
        ):
            return await resolve_principal_web_session(session, payload)

    def _in_scope(self, staff, pid: int) -> bool:
        vis = frozenset(int(x) for x in staff.get("org_visible_principal_ids") or [])
        res = resolve_resource_principal(SimpleNamespace(owner_principal_id=int(pid)))
        scoped = resource_in_principal_scope(
            actor_visible_principal_ids=vis, resolution=res
        )
        d = authorize(
            authenticated=True,
            ctx=authz_from_staff(staff),
            resource_principal_id=int(pid),
            pg_permission_ok=True,
            local_safety_ok=True,
        )
        return scoped and d.allowed

    # --- identity / session tampering ---

    async def test_principal_id_tamper_a1_to_a2(self) -> None:
        async with self.Session() as session:
            _o, _a, _a1, a2, _b, _b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB
            )
            assert auth
            with self.assertRaises(PrincipalWebIdentityError) as ctx:
                await self._resolve(
                    session, auth, {"org_principal_id": int(a2.id)}
                )
            self.assertEqual(ctx.exception.code, "principal_tamper")

    async def test_web_identity_id_tamper_to_sibling(self) -> None:
        async with self.Session() as session:
            _o, _a, a1, a2, _b, _b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB
            )
            assert auth
            ident_a2 = await session.get(
                type(auth.identity), int(auth.identity.id)
            )
            _ = ident_a2
            from app.services.principal_web_identity import get_web_identity_for_principal

            a2_ident = await get_web_identity_for_principal(session, int(a2.id))
            assert a2_ident is not None
            payload = build_principal_session_payload(auth)
            payload["web_identity_id"] = int(a2_ident.id)
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                with self.assertRaises(PrincipalWebIdentityError) as ctx:
                    await resolve_principal_web_session(session, payload)
            self.assertIn(ctx.exception.code, {"username_mismatch", "pv_mismatch"})
            self.assertEqual(int(auth.principal.id), int(a1.id))

    async def test_role_admin_tamper_does_not_become_owner(self) -> None:
        async with self.Session() as session:
            owner, _a, a1, _a2, _b, _b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB
            )
            assert auth
            with self.assertRaises(PrincipalWebIdentityError) as ctx:
                await resolve_principal_web_session(
                    session,
                    {
                        **build_principal_session_payload(auth),
                        "role": "admin",
                        "web_owner": True,
                        "org_depth": 0,
                        "org_principal_id": int(owner.id),
                    },
                )
            self.assertEqual(ctx.exception.code, "bad_role")
            staff = await self._resolve(session, auth)
            self.assertFalse(is_explicit_owner_staff(staff))
            self.assertFalse(is_explicit_org_owner(authz_from_staff(staff)))
            self.assertNotEqual(int(staff["org_principal_id"]), int(owner.id))
            _ = a1

    async def test_depth_parent_tamper_to_owner(self) -> None:
        async with self.Session() as session:
            owner, _a, _a1, _a2, _b, _b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB
            )
            assert auth
            with self.assertRaises(PrincipalWebIdentityError):
                await self._resolve(session, auth, {"org_depth": 0})
            with self.assertRaises(PrincipalWebIdentityError):
                await self._resolve(
                    session, auth, {"org_parent_id": None}
                )
            _ = owner

    async def test_legacy_role_admin_no_hierarchy(self) -> None:
        staff = {"role": "admin", "username": "legacy"}
        self.assertTrue(is_web_platform_admin(staff))
        self.assertFalse(is_explicit_owner_staff(staff))
        self.assertFalse(has_org_global_scope(authz_from_staff(staff)))
        self.assertFalse(is_platform_admin(staff))
        self.assertIsNone(shop_owner_id(staff))

    # --- hierarchy attacks ---

    async def test_l2_cannot_create_l3_l1_sibling(self) -> None:
        async with self.Session() as session:
            owner, a, a1, _a2, b, _b1 = await self._tree(session)
            vis = await visible_principal_ids(session, a1)
            staff = attach_org_principal_fields(
                {
                    "role": "principal",
                    "pg_can_create_admin": True,
                    "pg_actions": {"admins": {"create": True}},
                    "pg_is_owner": True,
                },
                a1,
                visible_principal_ids=vis,
            )
            with self.assertRaises(ChildProvisionError) as c1:
                await provision_level2_child(
                    session,
                    staff,
                    Level2ProvisionRequest(
                        pg_username="l3",
                        pg_password="AaBb12!@CdEf",
                        idempotency_key="l3",
                        depth=3,
                        parent_id=int(a1.id),
                    ),
                )
            self.assertIn(c1.exception.code, {"not_level1", "depth_forbidden"})
            with self.assertRaises(PrincipalProvisionError) as c2:
                await provision_level1_principal(
                    session,
                    staff,
                    Level1ProvisionRequest(
                        pg_username="new_l1",
                        pg_password="AaBb12!@CdEf",
                        idempotency_key="l1x",
                    ),
                )
            self.assertEqual(c2.exception.code, "not_owner")
            with self.assertRaises(OrgPrincipalError):
                await create_principal(
                    session, parent_id=int(a1.id), depth=2
                )
            with self.assertRaises(OrgPrincipalError):
                await create_principal(
                    session, parent_id=int(owner.id), depth=2
                )
            _ = (a, b)

    async def test_l1_a_cannot_create_under_b_or_promote(self) -> None:
        async with self.Session() as session:
            _o, a, _a1, _a2, b, _b1 = await self._tree(session)
            vis = await visible_principal_ids(session, a)
            staff = attach_org_principal_fields(
                {
                    "role": "principal",
                    "pg_can_create_admin": True,
                    "pg_actions": {"admins": {"create": True}},
                },
                a,
                visible_principal_ids=vis,
            )
            with patch(
                "app.services.pasarguard.get_pg_for_principal",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        create_admin=AsyncMock(return_value={}),
                        get_admin_roles=AsyncMock(
                            return_value=[{"id": 10, "name": "X", "is_owner": False}]
                        ),
                        delete_admin=AsyncMock(),
                    )
                ),
            ), patch(
                "app.services.principal_child_provisioning._owner_env_pg_username",
                return_value="env_owner_pg",
            ):
                result = await provision_level2_child(
                    session,
                    staff,
                    Level2ProvisionRequest(
                        pg_username="forced_under_b",
                        pg_password="AaBb12!@CdEf",
                        idempotency_key="under-b",
                        parent_id=int(b.id),
                        pg_role_id=10,
                    ),
                )
            self.assertEqual(int(result.principal.parent_id), int(a.id))
            self.assertNotEqual(int(result.principal.parent_id), int(b.id))
            with self.assertRaises(PrincipalLifecycleError):
                await reject_hierarchy_mutation(
                    staff, principal_id=int(a.id), parent_id=None, depth=0
                )

    # --- scope / IDOR ---

    async def test_scope_matrix_users_orders_tickets_services(self) -> None:
        async with self.Session() as session:
            owner, a, a1, a2, b, b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB
            )
            assert auth
            staff = await self._resolve(session, auth)
            self.assertTrue(self._in_scope(staff, int(a1.id)))
            self.assertFalse(self._in_scope(staff, int(a.id)))
            self.assertFalse(self._in_scope(staff, int(a2.id)))
            self.assertFalse(self._in_scope(staff, int(b.id)))
            self.assertFalse(self._in_scope(staff, int(b1.id)))
            self.assertFalse(self._in_scope(staff, int(owner.id)))

            vis = frozenset(int(x) for x in staff["org_visible_principal_ids"])
            for model, kwargs in (
                (BotUser, {"telegram_id": 7001, "referral_code": "r3e1", "role": "user"}),
                (Order, {"user_id": 1, "plan_id": 1, "price": 1, "status": "pending"}),
                (Ticket, {"user_id": 1, "subject": "s", "status": "open"}),
                (UserService, {"bot_user_id": 1, "plan_id": 1, "status": "active"}),
            ):
                for pid, expect in (
                    (int(a1.id), True),
                    (int(a.id), False),
                    (int(a2.id), False),
                    (int(b.id), False),
                ):
                    row = SimpleNamespace(owner_principal_id=pid)
                    res = resolve_resource_principal(row)
                    self.assertEqual(
                        resource_in_principal_scope(
                            actor_visible_principal_ids=vis, resolution=res
                        ),
                        expect,
                        msg=f"{model.__name__} pid={pid}",
                    )

    async def test_pg_object_idor_by_id_only(self) -> None:
        async with self.Session() as session:
            _o, _a, _a1, _a2, _b, _b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB
            )
            assert auth
            staff = await self._resolve(session, auth)
            staff = apply_principal_pg_local_safety(
                {
                    **staff,
                    "pg_admin_username": "l2_a1",
                    "pg_permissions": ["pg_users", "pg_hosts", "pg_nodes", "pg_templates", "pg_groups"],
                    "pg_actions": {
                        "users": {"update": True},
                        "hosts": {"update": True},
                        "nodes": {"update": True},
                        "templates": {"update": True},
                        "groups": {"update": True},
                    },
                    "pg_capabilities_ok": True,
                    "pg_credentials_ready": True,
                },
                _role("Broad", 1, BROAD),
            )
            mine = {"id": 1, "admin": {"username": "l2_a1"}}
            sib = {"id": 2, "admin": {"username": "l2_a2"}}
            parent = {"id": 3, "admin": {"username": "l1_a"}}
            other = {"id": 4, "admin": {"username": "l1_b"}}
            self.assertTrue(pg_user_in_staff_scope(mine, staff))
            self.assertFalse(pg_user_in_staff_scope(sib, staff))
            self.assertFalse(pg_object_in_staff_scope(sib, staff))
            self.assertFalse(pg_object_in_staff_scope(parent, staff))
            self.assertFalse(pg_object_in_staff_scope(other, staff))
            unknown = {"id": 9}
            self.assertFalse(pg_object_in_staff_scope(unknown, staff))
            self.assertFalse(pg_user_in_staff_scope({"id": 9}, staff))

    async def test_l1_scope_self_and_children_not_foreign(self) -> None:
        async with self.Session() as session:
            owner, a, a1, a2, b, b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l1_a", password=_WEB
            )
            assert auth
            staff = await self._resolve(session, auth)
            self.assertTrue(self._in_scope(staff, int(a.id)))
            self.assertTrue(self._in_scope(staff, int(a1.id)))
            self.assertTrue(self._in_scope(staff, int(a2.id)))
            self.assertFalse(self._in_scope(staff, int(b.id)))
            self.assertFalse(self._in_scope(staff, int(b1.id)))
            self.assertFalse(self._in_scope(staff, int(owner.id)))

    async def test_narrow_capability_denies_mutation_in_scope(self) -> None:
        staff = apply_principal_pg_local_safety(
            {
                "role": "principal",
                "org_principal_id": 11,
                "org_depth": 2,
                "org_parent_id": 10,
                "org_status": "active",
                "org_visible_principal_ids": [11],
                "pg_admin_username": "l2_a1",
                "pg_credentials_ready": True,
                "pg_capabilities_ok": True,
            },
            _role("Support", 2, NARROW),
        )
        from app.services.pg_access import enrich_staff_pg_from_role, map_pg_role_to_features

        role = _role("Support", 2, NARROW)
        staff = enrich_staff_pg_from_role(staff, map_pg_role_to_features(role), role)
        staff = apply_principal_pg_local_safety(staff, role)
        self.assertTrue(staff_may_pg_page(staff, "pg_users"))
        self.assertFalse(staff_pg_action(staff, "users", "update"))
        self.assertFalse(authorize_pg_action(staff, "users", "update").allowed)

    async def test_broad_permission_does_not_cross_scope(self) -> None:
        staff = apply_principal_pg_local_safety(
            {
                "role": "principal",
                "org_principal_id": 11,
                "org_depth": 2,
                "org_parent_id": 10,
                "org_status": "active",
                "org_visible_principal_ids": [11],
                "pg_admin_username": "l2_a1",
                "pg_credentials_ready": True,
                "pg_capabilities_ok": True,
            },
            _role("X", 3, BROAD),
        )
        from app.services.pg_access import enrich_staff_pg_from_role, map_pg_role_to_features

        role = _role("X", 3, BROAD)
        staff = enrich_staff_pg_from_role(staff, map_pg_role_to_features(role), role)
        staff = apply_principal_pg_local_safety(staff, role)
        d = authorize_pg_action(staff, "users", "update", resource_principal_id=12)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "resource_out_of_scope")

    # --- credentials ---

    async def test_l2_cannot_use_parent_owner_sibling_pg_client(self) -> None:
        from app.services import pasarguard as pg_mod

        async with self.Session() as session:
            _o, a, a1, a2, _b, _b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB
            )
            assert auth
            staff = await self._resolve(session, auth)
            self.assertEqual(staff.get("pg_admin_username"), "l2_a1")
            self.assertFalse(staff_uses_owner_pg_credentials(staff))
            payload = build_principal_session_payload(auth)
            self.assertFalse(session_contains_plaintext_secret(payload))
            self.assertNotIn("pg_password", payload)
            pg_mod._pg_principal_cache.clear()
            names: list[str] = []

            class _Client:
                def __init__(self, *, username=None, password=None, access_token=None):
                    names.append(username or "")
                    self.username = username
                    self._token = "t"
                    self._login_password = password

                async def ensure_token(self):
                    self._login_password = None
                    return self._token

            with patch.object(pg_mod, "PasarGuardClient", _Client):
                c = await get_pg_for_principal(session, principal_id=int(a1.id))
            self.assertEqual(c.username, "l2_a1")
            self.assertNotEqual(c.username, a.pg_username)
            self.assertNotEqual(c.username, a2.pg_username)
            self.assertNotIn((int(a1.id), "l1_a"), pg_mod._pg_principal_cache)

    async def test_pg_username_substitution_ignored_when_principal_id_set(self) -> None:
        from app.services import pasarguard as pg_mod

        async with self.Session() as session:
            _o, _a, a1, a2, _b, _b1 = await self._tree(session)
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
                c = await get_pg_for_principal(
                    session,
                    principal_id=int(a1.id),
                    pg_username=a2.pg_username,
                )
            self.assertEqual(c.username, "l2_a1")

    # --- state ---

    async def test_disabled_child_parent_fail_closed(self) -> None:
        async with self.Session() as session:
            owner, a, a1, _a2, _b, _b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB
            )
            assert auth
            payload = build_principal_session_payload(auth)
            a1.status = "disabled"
            await session.commit()
            with self.assertRaises(PrincipalWebIdentityError) as ctx:
                await authenticate_level1_web(
                    session, username="l2_a1", password=_WEB
                )
            self.assertEqual(ctx.exception.code, "principal_disabled")
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                with self.assertRaises(PrincipalWebIdentityError):
                    await resolve_principal_web_session(session, payload)
            with self.assertRaises(PasarGuardError):
                await get_pg_for_principal(session, principal_id=int(a1.id))

            a1.status = "active"
            a.status = "disabled"
            await session.commit()
            with self.assertRaises(PrincipalWebIdentityError) as ctx2:
                await authenticate_level1_web(
                    session, username="l2_a1", password=_WEB
                )
            self.assertEqual(ctx2.exception.code, "parent_disabled")
            with self.assertRaises(PasarGuardError):
                await get_pg_for_principal(session, principal_id=int(a1.id))
            self.assertEqual(
                await visible_principal_ids(session, a1), frozenset()
            )
            _ = owner

    async def test_disable_via_lifecycle_invalidates_session(self) -> None:
        async with self.Session() as session:
            owner, a, a1, _a2, _b, _b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB
            )
            assert auth
            payload = build_principal_session_payload(auth)
            await disable_level1_principal(session, _owner_staff(owner), int(a.id))
            await session.commit()
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                with self.assertRaises(PrincipalWebIdentityError) as ctx:
                    await resolve_principal_web_session(session, payload)
            self.assertEqual(ctx.exception.code, "parent_disabled")
            _ = a1

    # --- cache / outage ---

    async def test_cookie_pg_role_id_must_not_apply_when_live_lookup_fails(self) -> None:
        """Fail closed on PG outage — cookie role_id + shared role cache must not escalate."""
        from app.services import pg_access as pg_acc

        async with self.Session() as session:
            _o, _a, _a1, _a2, _b, _b1 = await self._tree(session)
            auth = await authenticate_level1_web(
                session, username="l2_a1", password=_WEB
            )
            assert auth
            broad = _role("Operator", 99, BROAD)
            from app.services.pg_access import map_pg_role_to_features
            from app.services.pg_read_cache import ROLE_FEATURES_PATH, cache_put

            # Poison a foreign identity; principal path must still fail closed.
            cache_put(
                "owner@https://pg.example:platform",
                ROLE_FEATURES_PATH,
                {"role_id": 99},
                {
                    "ok": True,
                    "features": map_pg_role_to_features(broad),
                    "role": broad,
                },
            )
            payload = build_principal_session_payload(auth)
            payload["pg_role_id"] = 99
            payload["pg_permissions"] = ["pg_nodes", "pg_users", "pg_admins"]
            payload["pg_actions"] = {"nodes": {"update": True}}
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                staff = await resolve_principal_web_session(session, payload)
            self.assertFalse(staff.get("pg_capabilities_ok"))
            self.assertFalse(staff_may_pg_page(staff, "pg_nodes"))
            self.assertFalse(staff_pg_action(staff, "nodes", "update"))

    async def test_missing_invalid_principal_parent_depth_deny(self) -> None:
        self.assertEqual(
            authorize(
                authenticated=True,
                ctx=authz_from_staff({"role": "principal"}),
                resource_principal_id=1,
                pg_permission_ok=True,
            ).allowed,
            False,
        )
        async with self.Session() as session:
            self.assertEqual(
                await visible_principal_ids(session, None), frozenset()
            )
            with self.assertRaises(OrgPrincipalError):
                await create_principal(session, parent_id=None, depth=1)
            with self.assertRaises(PasarGuardError):
                await get_pg_for_principal(session, principal_id=99999)

    async def test_pg_unavailable_empty_capabilities(self) -> None:
        staff = apply_principal_pg_local_safety(
            {
                "role": "principal",
                "org_principal_id": 11,
                "org_depth": 2,
                "org_parent_id": 10,
                "org_status": "active",
                "org_visible_principal_ids": [11],
                "pg_admin_username": "l2_a1",
                "pg_credentials_ready": True,
                "pg_capabilities_ok": False,
                "pg_permissions": ["pg_users"],
            },
            None,
        )
        staff["pg_capabilities_ok"] = False
        self.assertFalse(authorize_pg_page(staff, "pg_users").allowed)

    # --- legacy bot / reseller / pg_staff ---

    async def test_bot_role_admin_not_owner_without_admin_ids(self) -> None:
        user = BotUser(telegram_id=4242, role="admin", referral_code="botadm")
        self.assertTrue(bot_is_platform_admin(user))
        async with self.Session() as session:
            await ensure_owner_principal(session)
            with patch(
                "app.config.get_settings",
                return_value=SimpleNamespace(admin_ids=frozenset()),
            ):
                self.assertFalse(
                    await is_bot_owner_principal(session, user, is_reseller_bot=False)
                )
            self.assertFalse(
                await is_bot_owner_principal(session, user, is_reseller_bot=True)
            )

    async def test_reseller_cookie_org_principal_id_must_not_select_owner(self) -> None:
        """Defense in depth: untrusted org_principal_id on reseller staff."""
        async with self.Session() as session:
            owner, a, _a1, _a2, _b, _b1 = await self._tree(session)
            spoof = {
                "role": "reseller",
                "bot_user_id": 99,
                "org_principal_id": int(owner.id),
                "org_depth": 0,
            }
            resolved = await resolve_org_principal_for_staff(session, spoof)
            # Must not become Owner via cookie principal_id.
            if resolved is not None:
                self.assertFalse(is_owner_principal(resolved))
                self.assertNotEqual(int(resolved.id), int(owner.id))
            spoof_flag = {
                "role": "reseller",
                "bot_user_id": 99,
                "web_owner": True,
            }
            flagged = await resolve_org_principal_for_staff(session, spoof_flag)
            if flagged is not None:
                self.assertFalse(is_owner_principal(flagged))
                self.assertNotEqual(int(flagged.id), int(owner.id))
            _ = a

    async def test_pg_staff_without_mapping_none(self) -> None:
        async with self.Session() as session:
            await ensure_owner_principal(session)
            got = await resolve_org_principal_for_staff(
                session, {"role": "pg_staff", "pg_staff_id": 12345}
            )
            self.assertIsNone(got)

    # --- idempotency ---

    async def test_l2_idempotency_not_reusable_by_sibling(self) -> None:
        async with self.Session() as session:
            _o, a, _a1, _a2, b, _b1 = await self._tree(session)
            pg = SimpleNamespace(
                create_admin=AsyncMock(return_value={}),
                delete_admin=AsyncMock(),
                get_admin_roles=AsyncMock(
                    return_value=[{"id": 10, "name": "X", "is_owner": False}]
                ),
            )
            staff_a = attach_org_principal_fields(
                {
                    "role": "principal",
                    "pg_can_create_admin": True,
                    "pg_actions": {"admins": {"create": True}},
                },
                a,
                visible_principal_ids=await visible_principal_ids(session, a),
            )
            staff_b = attach_org_principal_fields(
                {
                    "role": "principal",
                    "pg_can_create_admin": True,
                    "pg_actions": {"admins": {"create": True}},
                },
                b,
                visible_principal_ids=await visible_principal_ids(session, b),
            )
            key = "shared-idem"
            with patch(
                "app.services.pasarguard.get_pg_for_principal",
                new=AsyncMock(return_value=pg),
            ), patch(
                "app.services.principal_child_provisioning._owner_env_pg_username",
                return_value="env_owner_pg",
            ):
                r1 = await provision_level2_child(
                    session,
                    staff_a,
                    Level2ProvisionRequest(
                        pg_username="idem_a_child",
                        pg_password="AaBb12!@CdEf",
                        idempotency_key=key,
                        pg_role_id=10,
                    ),
                )
                await session.commit()
                r2 = await provision_level2_child(
                    session,
                    staff_b,
                    Level2ProvisionRequest(
                        pg_username="idem_b_child",
                        pg_password="AaBb12!@CdEf",
                        idempotency_key=key,
                        pg_role_id=10,
                    ),
                )
            self.assertNotEqual(int(r1.principal.id), int(r2.principal.id))
            self.assertEqual(int(r1.principal.parent_id), int(a.id))
            self.assertEqual(int(r2.principal.parent_id), int(b.id))
            self.assertNotEqual(
                scoped_idempotency_key(int(a.id), key),
                scoped_idempotency_key(int(b.id), key),
            )

    async def test_l1_idempotency_must_not_return_l2_row(self) -> None:
        """Owner L1 replay of an L2 scoped key must not yield a depth-2 Principal."""
        async with self.Session() as session:
            owner, a, _a1, _a2, _b, _b1 = await self._tree(session)
            pg = SimpleNamespace(
                create_admin=AsyncMock(return_value={}),
                delete_admin=AsyncMock(),
                get_admin_roles=AsyncMock(
                    return_value=[{"id": 10, "name": "X", "is_owner": False}]
                ),
            )
            staff_a = attach_org_principal_fields(
                {
                    "role": "principal",
                    "pg_can_create_admin": True,
                    "pg_actions": {"admins": {"create": True}},
                },
                a,
                visible_principal_ids=await visible_principal_ids(session, a),
            )
            client_key = "cross-api"
            with patch(
                "app.services.pasarguard.get_pg_for_principal",
                new=AsyncMock(return_value=pg),
            ), patch(
                "app.services.principal_child_provisioning._owner_env_pg_username",
                return_value="env_owner_pg",
            ):
                child = await provision_level2_child(
                    session,
                    staff_a,
                    Level2ProvisionRequest(
                        pg_username="cross_child",
                        pg_password="AaBb12!@CdEf",
                        idempotency_key=client_key,
                        pg_role_id=10,
                    ),
                )
                await session.commit()
            stolen = scoped_idempotency_key(int(a.id), client_key)
            owner_staff = _owner_staff(owner)
            owner_staff["pg_is_owner"] = True
            owner_staff["pg_permissions"] = ["pg_admins"]
            with patch(
                "app.services.pasarguard.get_pg",
                return_value=pg,
            ), patch(
                "app.services.principal_provisioning._owner_env_pg_username",
                return_value="env_owner_pg",
            ):
                with self.assertRaises(PrincipalProvisionError):
                    await provision_level1_principal(
                        session,
                        owner_staff,
                        Level1ProvisionRequest(
                            pg_username="should_not_replay",
                            pg_password="AaBb12!@CdEf",
                            idempotency_key=stolen,
                            pg_role_id=10,
                        ),
                    )
            self.assertEqual(int(child.principal.depth), 2)

    async def test_l2_cannot_replay_l1_idempotency_key(self) -> None:
        async with self.Session() as session:
            _o, a, a1, _a2, _b, _b1 = await self._tree(session)
            vis = await visible_principal_ids(session, a1)
            staff = attach_org_principal_fields(
                {"role": "principal", "pg_can_create_admin": True},
                a1,
                visible_principal_ids=vis,
            )
            with self.assertRaises(PrincipalProvisionError):
                await provision_level1_principal(
                    session,
                    staff,
                    Level1ProvisionRequest(
                        pg_username="x",
                        pg_password="AaBb12!@CdEf",
                        idempotency_key="prov-a-1",
                    ),
                )
            _ = a


if __name__ == "__main__":
    unittest.main()
