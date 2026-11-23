"""Phase 2D — Level-1 Principal lifecycle (Owner list/disable/enable) tests."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import BotUser, OrgPrincipal, OrgPrincipalProvision
from app.services.org_principals import (
    DEPTH_ONE,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    attach_org_principal_fields,
    create_principal,
    ensure_owner_principal,
    is_owner_principal,
)
from app.services.principal_lifecycle import (
    PrincipalLifecycleError,
    deactivate_level1_principal,
    disable_level1_principal,
    enable_level1_principal,
    get_level1_principal_detail,
    hard_delete_level1_principal,
    list_level1_principals,
    public_views_contain_secret,
    reject_hierarchy_mutation,
)
from app.services.principal_pg_authz import authorize_pg_page, apply_level1_pg_local_safety
from app.services.principal_provisioning import (
    Level1ProvisionRequest,
    PrincipalProvisionError,
    provision_level1_principal,
)
from app.services.principal_web_identity import (
    ROLE_PRINCIPAL,
    PrincipalWebIdentityError,
    attach_level1_web_identity,
    authenticate_level1_web,
    build_principal_session_payload,
    resolve_principal_web_session,
)
from app.services.pg_access import enrich_staff_pg_from_role, map_pg_role_to_features
from app.services.secret_box import encrypt_secret
from tests.postgres_support import scoped_async_engine


_PG_PASSWORD = "AaBb12!@CdEf"
_WEB_PASSWORD = "WebLogin12!@Ab"
_PLAIN_A = "SecretA12!@xx"


def _owner_staff(owner: OrgPrincipal, *, pg_capable: bool = True) -> dict:
    staff = attach_org_principal_fields(
        {"role": "admin", "web_owner": True, "username": "owner"},
        owner,
        visible_principal_ids=frozenset({int(owner.id)}),
    )
    if pg_capable:
        staff["pg_is_owner"] = True
        staff["pg_permissions"] = ["pg_admins", "pg_users", "pg_overview"]
    else:
        staff["pg_is_owner"] = False
        staff["pg_permissions"] = ["pg_users"]
    return staff


def _level1_staff(principal: OrgPrincipal, owner: OrgPrincipal) -> dict:
    return attach_org_principal_fields(
        {
            "role": ROLE_PRINCIPAL,
            "username": f"web_{principal.pg_username}",
            "web_identity_id": 1,
            "pg_admin_username": principal.pg_username,
            "pg_permissions": ["pg_users"],
            "pg_is_owner": False,
            "web_owner": False,
        },
        principal,
        visible_principal_ids=frozenset({int(principal.id)}),
    )


class Phase2DLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _seed_abc(self, session) -> tuple[OrgPrincipal, OrgPrincipal, OrgPrincipal, OrgPrincipal]:
        owner = await ensure_owner_principal(session)
        a = await create_principal(
            session,
            parent_id=int(owner.id),
            depth=1,
            pg_username="prin_a",
            pg_password_enc=encrypt_secret(_PLAIN_A),
        )
        b = await create_principal(
            session,
            parent_id=int(owner.id),
            depth=1,
            pg_username="prin_b",
            pg_password_enc=encrypt_secret("SecretB34!@yy"),
        )
        c = await create_principal(
            session,
            parent_id=int(owner.id),
            depth=1,
            pg_username="prin_c",
            pg_password_enc=encrypt_secret("SecretC56!@zz"),
        )
        session.add(
            OrgPrincipalProvision(
                idempotency_key="seed-a",
                principal_id=int(a.id),
                pg_username="prin_a",
                pg_role_id=10,
                created_by_principal_id=int(owner.id),
                status="completed",
            )
        )
        await session.commit()
        return owner, a, b, c

    async def test_a_owner_can_list_level1(self) -> None:
        async with self.Session() as session:
            owner, a, b, c = await self._seed_abc(session)
            views = await list_level1_principals(session, _owner_staff(owner))
            ids = {v.principal_id for v in views}
            self.assertEqual(ids, {int(a.id), int(b.id), int(c.id)})
            self.assertTrue(all(v.depth == 1 for v in views))
            self.assertTrue(all(v.parent_id == int(owner.id) for v in views))

    async def test_b_owner_can_view_detail(self) -> None:
        async with self.Session() as session:
            owner, a, _, _ = await self._seed_abc(session)
            with patch(
                "app.services.principal_lifecycle._safe_pg_role_name",
                new=AsyncMock(return_value="Operator"),
            ):
                detail = await get_level1_principal_detail(
                    session, _owner_staff(owner), int(a.id)
                )
            self.assertEqual(detail.principal_id, int(a.id))
            self.assertEqual(detail.pg_username, "prin_a")
            self.assertEqual(detail.pg_role_id, 10)
            self.assertEqual(detail.pg_role_name, "Operator")
            self.assertEqual(detail.status, STATUS_ACTIVE)
            self.assertEqual(detail.web_identity_status, "none")

    async def test_c_non_owner_cannot_list(self) -> None:
        async with self.Session() as session:
            owner, a, _, _ = await self._seed_abc(session)
            with self.assertRaises(PrincipalLifecycleError) as ctx:
                await list_level1_principals(session, _level1_staff(a, owner))
            self.assertEqual(ctx.exception.code, "not_owner")

    async def test_d_non_owner_cannot_inspect_sibling(self) -> None:
        async with self.Session() as session:
            owner, a, b, _ = await self._seed_abc(session)
            with self.assertRaises(PrincipalLifecycleError) as ctx:
                await get_level1_principal_detail(
                    session, _level1_staff(a, owner), int(b.id)
                )
            self.assertEqual(ctx.exception.code, "not_owner")

    async def test_e_owner_can_disable(self) -> None:
        async with self.Session() as session:
            owner, a, _, _ = await self._seed_abc(session)
            view = await disable_level1_principal(
                session, _owner_staff(owner), int(a.id)
            )
            await session.commit()
            self.assertEqual(view.status, STATUS_DISABLED)
            await session.refresh(a)
            self.assertEqual(a.status, STATUS_DISABLED)

    async def test_f_disabled_principal_login_deny(self) -> None:
        async with self.Session() as session:
            owner, a, _, _ = await self._seed_abc(session)
            await attach_level1_web_identity(
                session,
                principal_id=int(a.id),
                web_username="prin_a",
                password=_WEB_PASSWORD,
            )
            await disable_level1_principal(session, _owner_staff(owner), int(a.id))
            await session.commit()
            with self.assertRaises(PrincipalWebIdentityError) as ctx:
                await authenticate_level1_web(
                    session, username="prin_a", password=_WEB_PASSWORD
                )
            self.assertEqual(ctx.exception.code, "principal_disabled")

    async def test_g_disabled_session_deny_on_resolve(self) -> None:
        async with self.Session() as session:
            owner, a, _, _ = await self._seed_abc(session)
            await attach_level1_web_identity(
                session,
                principal_id=int(a.id),
                web_username="prin_a",
                password=_WEB_PASSWORD,
            )
            await session.commit()
            auth = await authenticate_level1_web(
                session, username="prin_a", password=_WEB_PASSWORD
            )
            assert auth is not None
            payload = build_principal_session_payload(auth)
            # Disable after cookie issued
            await disable_level1_principal(session, _owner_staff(owner), int(a.id))
            await session.commit()
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                with self.assertRaises(PrincipalWebIdentityError) as ctx:
                    await resolve_principal_web_session(session, payload)
            self.assertEqual(ctx.exception.code, "principal_disabled")

    async def test_h_disabled_cannot_use_pg(self) -> None:
        from app.services import pasarguard as pg_mod
        from app.services.pasarguard import PasarGuardError, get_pg_for_principal

        async with self.Session() as session:
            owner, a, _, _ = await self._seed_abc(session)
            await session.commit()
            pg_mod._pg_principal_cache.clear()

            class _Client:
                def __init__(self, *, username=None, password=None, access_token=None):
                    self._token = "t"
                    self._login_password = password

                async def ensure_token(self):
                    self._login_password = None
                    return self._token

            with patch.object(pg_mod, "PasarGuardClient", _Client):
                client = await get_pg_for_principal(session, principal_id=int(a.id))
            self.assertIsNotNone(client)
            # Cache populated then disable must invalidate + deny
            await disable_level1_principal(session, _owner_staff(owner), int(a.id))
            await session.commit()
            self.assertNotIn(
                (int(a.id), "prin_a"),
                {(k[0], k[1]) for k in pg_mod._pg_principal_cache},
            )
            with self.assertRaises(PasarGuardError):
                await get_pg_for_principal(session, principal_id=int(a.id))

    async def test_i_owner_can_reenable(self) -> None:
        async with self.Session() as session:
            owner, a, _, _ = await self._seed_abc(session)
            await disable_level1_principal(session, _owner_staff(owner), int(a.id))
            await session.commit()
            view = await enable_level1_principal(
                session, _owner_staff(owner), int(a.id)
            )
            await session.commit()
            self.assertEqual(view.status, STATUS_ACTIVE)

    async def test_j_reenabled_retains_identity(self) -> None:
        async with self.Session() as session:
            owner, a, _, _ = await self._seed_abc(session)
            await attach_level1_web_identity(
                session,
                principal_id=int(a.id),
                web_username="prin_a",
                password=_WEB_PASSWORD,
            )
            pid = int(a.id)
            parent = int(a.parent_id) if a.parent_id is not None else None
            depth = int(a.depth)
            uname = a.pg_username
            await disable_level1_principal(session, _owner_staff(owner), pid)
            await session.commit()
            await enable_level1_principal(session, _owner_staff(owner), pid)
            await session.commit()
            await session.refresh(a)
            self.assertEqual(int(a.id), pid)
            self.assertEqual(a.parent_id, parent)
            self.assertEqual(int(a.depth), depth)
            self.assertEqual(a.pg_username, uname)
            auth = await authenticate_level1_web(
                session, username="prin_a", password=_WEB_PASSWORD
            )
            self.assertIsNotNone(auth)
            assert auth is not None
            self.assertEqual(int(auth.principal.id), pid)

    async def test_k_cannot_create_second_owner(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            await session.commit()
            with self.assertRaises(Exception):
                await create_principal(
                    session,
                    parent_id=None,
                    depth=0,
                    status=STATUS_ACTIVE,
                )

    async def test_l_cannot_change_parent_depth_to_owner(self) -> None:
        async with self.Session() as session:
            owner, a, _, _ = await self._seed_abc(session)
            with self.assertRaises(PrincipalLifecycleError) as ctx:
                await reject_hierarchy_mutation(
                    _owner_staff(owner),
                    principal_id=int(a.id),
                    parent_id=None,
                    depth=0,
                    promote_to_owner=True,
                )
            self.assertIn(ctx.exception.code, {"hierarchy_immutable", "cannot_become_owner"})
            # Sibling also cannot mutate
            with self.assertRaises(PrincipalLifecycleError):
                await reject_hierarchy_mutation(
                    _level1_staff(a, owner),
                    principal_id=int(a.id),
                    parent_id=None,
                    depth=0,
                )

    async def test_m_principal_cannot_disable_sibling(self) -> None:
        async with self.Session() as session:
            owner, a, b, _ = await self._seed_abc(session)
            with self.assertRaises(PrincipalLifecycleError) as ctx:
                await disable_level1_principal(
                    session, _level1_staff(a, owner), int(b.id)
                )
            self.assertEqual(ctx.exception.code, "not_owner")

    async def test_n_principal_cannot_create_level1(self) -> None:
        async with self.Session() as session:
            owner, a, _, _ = await self._seed_abc(session)
            with patch(
                "app.services.pasarguard.get_pg",
                return_value=SimpleNamespace(
                    create_admin=AsyncMock(),
                    get_admin_roles=AsyncMock(
                        return_value=[{"id": 10, "name": "X", "is_owner": False}]
                    ),
                    delete_admin=AsyncMock(),
                ),
            ), patch(
                "app.services.principal_provisioning._owner_env_pg_username",
                return_value="env_owner",
            ):
                with self.assertRaises(PrincipalProvisionError) as ctx:
                    await provision_level1_principal(
                        session,
                        _level1_staff(a, owner),
                        Level1ProvisionRequest(
                            pg_username="evil_new",
                            pg_password=_PG_PASSWORD,
                            idempotency_key="evil-1",
                            pg_role_id=10,
                        ),
                    )
            self.assertEqual(ctx.exception.code, "not_owner")

    async def test_o_pg_role_names_do_not_affect_hierarchy(self) -> None:
        async with self.Session() as session:
            owner, a, _, _ = await self._seed_abc(session)
            # Even with PG role name "Owner" / "Administrator", depth stays 1
            self.assertEqual(int(a.depth), DEPTH_ONE)
            self.assertFalse(is_owner_principal(a))
            staff = _level1_staff(a, owner)
            staff["pg_role_name"] = "Owner"
            staff["pg_role"] = {"name": "Administrator", "is_owner": True}
            self.assertFalse(is_owner_principal(a))
            self.assertEqual(int(staff["org_depth"]), 1)
            self.assertFalse(bool(staff.get("web_owner")))

    async def test_p_pg_capabilities_after_role_change(self) -> None:
        role_users = {
            "id": 10,
            "name": "Operator",
            "is_owner": False,
            "permissions": {
                "users": {
                    "read": True,
                    "read_simple": True,
                    "create": False,
                    "update": False,
                    "delete": False,
                }
            },
        }
        role_hosts = {
            "id": 10,
            "name": "Operator",
            "is_owner": False,
            "permissions": {
                "hosts": {
                    "read": True,
                    "create": False,
                    "update": False,
                    "delete": False,
                }
            },
        }
        features = map_pg_role_to_features(role_users)
        staff = {
            "role": ROLE_PRINCIPAL,
            "org_principal_id": 2,
            "org_depth": 1,
            "org_parent_id": 1,
            "org_status": "active",
            "org_visible_principal_ids": [2],
            "web_owner": False,
        }
        staff = enrich_staff_pg_from_role(staff, features, role_users)
        staff = apply_level1_pg_local_safety(staff, role_users)
        self.assertTrue(authorize_pg_page(staff, "pg_users").allowed)

        # External role change → remap capabilities (no permanent matrix)
        features2 = map_pg_role_to_features(role_hosts)
        staff2 = enrich_staff_pg_from_role(dict(staff), features2, role_hosts)
        staff2 = apply_level1_pg_local_safety(staff2, role_hosts)
        self.assertFalse(authorize_pg_page(staff2, "pg_users").allowed)
        self.assertTrue(authorize_pg_page(staff2, "pg_hosts").allowed)

    async def test_q_no_credential_in_api_response(self) -> None:
        async with self.Session() as session:
            owner, a, _, _ = await self._seed_abc(session)
            views = await list_level1_principals(session, _owner_staff(owner))
            detail = await get_level1_principal_detail(
                session, _owner_staff(owner), int(a.id)
            )
            self.assertFalse(public_views_contain_secret(views))
            self.assertFalse(public_views_contain_secret(detail))
            blob = str(detail.to_public_dict()) + str([v.to_public_dict() for v in views])
            self.assertNotIn(_PLAIN_A, blob)
            self.assertNotIn("pg_password", blob)
            self.assertNotIn("pg_password_enc", blob)
            self.assertNotIn("gAAAAA", blob)

    async def test_cannot_disable_owner(self) -> None:
        async with self.Session() as session:
            owner, _, _, _ = await self._seed_abc(session)
            with self.assertRaises(PrincipalLifecycleError) as ctx:
                await disable_level1_principal(
                    session, _owner_staff(owner), int(owner.id)
                )
            self.assertEqual(ctx.exception.code, "cannot_manage_owner")

    async def test_hard_delete_blocked_when_owns_resources(self) -> None:
        async with self.Session() as session:
            owner, a, _, _ = await self._seed_abc(session)
            session.add(
                BotUser(
                    telegram_id=999001,
                    role="user",
                    referral_code="ref_2d_a",
                    owner_principal_id=int(a.id),
                )
            )
            await session.commit()
            await deactivate_level1_principal(
                session, _owner_staff(owner), int(a.id)
            )
            await session.commit()
            with self.assertRaises(PrincipalLifecycleError) as ctx:
                await hard_delete_level1_principal(
                    session,
                    _owner_staff(owner),
                    int(a.id),
                    confirm=True,
                )
            self.assertEqual(ctx.exception.code, "owns_resources")

    async def test_hard_delete_requires_confirm_and_disable(self) -> None:
        async with self.Session() as session:
            owner, a, _, _ = await self._seed_abc(session)
            with self.assertRaises(PrincipalLifecycleError) as ctx:
                await hard_delete_level1_principal(
                    session, _owner_staff(owner), int(a.id), confirm=False
                )
            self.assertEqual(ctx.exception.code, "confirm_required")
            with self.assertRaises(PrincipalLifecycleError) as ctx2:
                await hard_delete_level1_principal(
                    session, _owner_staff(owner), int(a.id), confirm=True
                )
            self.assertEqual(ctx2.exception.code, "must_disable_first")


if __name__ == "__main__":
    unittest.main()
