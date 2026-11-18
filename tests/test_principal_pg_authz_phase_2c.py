"""Phase 2C — PasarGuard capability → Web authorization for Level-1 Principals."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401
from app.services.authz import authz_from_staff, can_pg_page
from app.services.org_principals import (
    attach_org_principal_fields,
    create_principal,
    ensure_owner_principal,
)
from app.services.org_scope import visible_principal_ids
from app.services.pg_access import (
    enrich_staff_pg_from_role,
    map_pg_role_to_features,
    staff_pg_action,
)
from app.services.pg_user_scope import filter_pg_users_for_staff, pg_user_in_staff_scope
from app.services.principal_pg_authz import (
    apply_level1_pg_local_safety,
    authorize_pg_action,
    authorize_pg_page,
    staff_may_pg_action,
    staff_may_pg_page,
    ui_pg_action_flags,
)
from app.services.principal_web_identity import ROLE_PRINCIPAL
from tests.postgres_support import scoped_async_engine


def _role_matrix(
    *,
    name: str,
    role_id: int,
    permissions: dict,
    is_owner: bool = False,
) -> dict:
    return {
        "id": role_id,
        "name": name,
        "is_owner": is_owner,
        "permissions": permissions,
    }


def _level1_staff(
    principal_id: int,
    *,
    parent_id: int,
    pg_username: str,
    role: dict,
    sibling_visible: list[int] | None = None,
) -> dict:
    features = map_pg_role_to_features(role)
    staff = {
        "role": ROLE_PRINCIPAL,
        "username": f"web_{pg_username}",
        "web_identity_id": principal_id,
        "pg_admin_username": pg_username,
        "pg_credentials_ready": True,
        "org_principal_id": principal_id,
        "org_depth": 1,
        "org_parent_id": parent_id,
        "org_status": "active",
        "org_visible_principal_ids": sibling_visible or [principal_id],
        "web_owner": False,
        "pg_is_owner": False,
    }
    staff = enrich_staff_pg_from_role(staff, features, role)
    return apply_level1_pg_local_safety(staff, role)


USERS_VIEW_ONLY = {
    "users": {"read": True, "read_simple": True, "create": False, "update": False, "delete": False},
}
USERS_VIEW_UPDATE = {
    "users": {"read": True, "update": True, "create": False, "delete": False},
}
NODES_VIEW_ONLY = {
    "nodes": {"read": True, "read_simple": True, "update": False, "create": False, "delete": False},
}
TEMPLATES_VIEW_ONLY = {
    "templates": {
        "read": True,
        "read_simple": True,
        "update": False,
        "create": False,
        "delete": False,
    },
}
BROAD = {
    "users": {"read": True, "create": True, "update": True, "delete": True},
    "nodes": {"read": True, "update": True, "create": True, "delete": True, "reconnect": True},
    "templates": {"read": True, "update": True, "create": True, "delete": True},
    "groups": {"read": True, "update": True, "create": True, "delete": True},
    "hosts": {"read": True, "update": True, "create": True, "delete": True},
}


class Phase2CCapabilityTests(unittest.TestCase):
    def test_a_users_view_allows_page(self) -> None:
        role = _role_matrix(name="CustomView", role_id=50, permissions=USERS_VIEW_ONLY)
        staff = _level1_staff(10, parent_id=1, pg_username="a_pg", role=role)
        self.assertTrue(staff_may_pg_page(staff, "pg_users"))
        self.assertTrue(authorize_pg_page(staff, "pg_users").allowed)

    def test_b_without_users_view_deny(self) -> None:
        role = _role_matrix(name="NoUsers", role_id=51, permissions=NODES_VIEW_ONLY)
        staff = _level1_staff(10, parent_id=1, pg_username="b_pg", role=role)
        self.assertFalse(staff_may_pg_page(staff, "pg_users"))
        self.assertFalse(authorize_pg_page(staff, "pg_users").allowed)

    def test_c_view_without_update(self) -> None:
        role = _role_matrix(name="ViewOnly", role_id=52, permissions=USERS_VIEW_ONLY)
        staff = _level1_staff(10, parent_id=1, pg_username="c_pg", role=role)
        self.assertTrue(staff_may_pg_page(staff, "pg_users"))
        self.assertTrue(staff_may_pg_action(staff, "users", "read") or can_pg_page(authz_from_staff(staff), "pg_users"))
        # read is via page; update action must deny
        self.assertFalse(staff_pg_action(staff, "users", "update"))
        self.assertFalse(staff_may_pg_action(staff, "users", "update"))
        self.assertFalse(authorize_pg_action(staff, "users", "update").allowed)

    def test_d_nodes_view_without_update(self) -> None:
        role = _role_matrix(name="NodeView", role_id=53, permissions=NODES_VIEW_ONLY)
        staff = _level1_staff(10, parent_id=1, pg_username="d_pg", role=role)
        self.assertTrue(staff_may_pg_page(staff, "pg_nodes"))
        self.assertFalse(staff_pg_action(staff, "nodes", "update"))
        self.assertFalse(staff_may_pg_action(staff, "nodes", "update"))

    def test_e_templates_view_without_update(self) -> None:
        role = _role_matrix(name="TplView", role_id=54, permissions=TEMPLATES_VIEW_ONLY)
        staff = _level1_staff(10, parent_id=1, pg_username="e_pg", role=role)
        self.assertTrue(staff_may_pg_page(staff, "pg_templates"))
        self.assertFalse(staff_pg_action(staff, "templates", "update"))

    def test_f_administrator_name_limited(self) -> None:
        role = _role_matrix(
            name="Administrator",
            role_id=55,
            permissions=USERS_VIEW_ONLY,
        )
        staff = _level1_staff(10, parent_id=1, pg_username="f_pg", role=role)
        self.assertTrue(staff_may_pg_page(staff, "pg_users"))
        self.assertFalse(staff_may_pg_action(staff, "users", "update"))
        self.assertFalse(staff_may_pg_page(staff, "pg_nodes"))
        self.assertFalse(staff_may_pg_page(staff, "pg_admins"))

    def test_g_operator_name_broad_but_local_policy(self) -> None:
        role = _role_matrix(name="Operator", role_id=56, permissions=BROAD)
        staff = _level1_staff(10, parent_id=1, pg_username="g_pg", role=role)
        self.assertTrue(staff_may_pg_page(staff, "pg_users"))
        self.assertTrue(staff_may_pg_action(staff, "users", "update"))
        # Local safety: never Owner admin management
        self.assertFalse(staff_may_pg_page(staff, "pg_admins"))
        self.assertFalse(staff_pg_action(staff, "admins", "create"))
        self.assertFalse(bool(staff.get("pg_is_owner")))
        self.assertFalse(bool(staff.get("web_owner")))

    def test_h_arbitrary_custom_role_name(self) -> None:
        role = _role_matrix(
            name="RoleX_Custom_99",
            role_id=57,
            permissions={**USERS_VIEW_UPDATE, **NODES_VIEW_ONLY},
        )
        staff = _level1_staff(10, parent_id=1, pg_username="h_pg", role=role)
        self.assertTrue(staff_may_pg_page(staff, "pg_users"))
        self.assertTrue(staff_may_pg_action(staff, "users", "update"))
        self.assertTrue(staff_may_pg_page(staff, "pg_nodes"))
        self.assertFalse(staff_may_pg_action(staff, "nodes", "update"))

    def test_i_same_role_different_scope(self) -> None:
        role = _role_matrix(name="Shared", role_id=58, permissions=BROAD)
        a = _level1_staff(10, parent_id=1, pg_username="a_shared", role=role)
        b = _level1_staff(20, parent_id=1, pg_username="b_shared", role=role)
        # Same capabilities
        self.assertEqual(a["pg_permissions"], b["pg_permissions"])
        self.assertTrue(staff_may_pg_action(a, "users", "update"))
        self.assertTrue(staff_may_pg_action(b, "users", "update"))
        # Different scope
        self.assertEqual(a["org_visible_principal_ids"], [10])
        self.assertEqual(b["org_visible_principal_ids"], [20])
        d = authorize_pg_action(a, "users", "update", resource_principal_id=20)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "resource_out_of_scope")

    def test_j_pg_permission_does_not_override_scope(self) -> None:
        role = _role_matrix(name="Viewer", role_id=59, permissions=BROAD)
        a = _level1_staff(10, parent_id=1, pg_username="scope_a", role=role)
        # PG user owned by B
        user_b = {"id": 1, "admin": {"username": "scope_b"}}
        self.assertFalse(pg_user_in_staff_scope(user_b, a))
        filtered = filter_pg_users_for_staff(
            [user_b, {"id": 2, "admin": {"username": "scope_a"}}], a
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["id"], 2)

    def test_k_missing_pg_identity_deny(self) -> None:
        staff = {
            "role": ROLE_PRINCIPAL,
            "org_principal_id": 10,
            "org_depth": 1,
            "org_parent_id": 1,
            "org_status": "active",
            "org_visible_principal_ids": [10],
            "pg_admin_username": None,
            "pg_permissions": [],
            "pg_credentials_ready": False,
        }
        self.assertFalse(staff_may_pg_page(staff, "pg_users"))
        self.assertFalse(authorize_pg_page(staff, "pg_users").allowed)

    def test_l_pg_unavailable_empty_capabilities_deny(self) -> None:
        # Empty matrix after failed role resolve
        staff = apply_level1_pg_local_safety(
            {
                "role": ROLE_PRINCIPAL,
                "org_principal_id": 10,
                "org_depth": 1,
                "org_parent_id": 1,
                "org_status": "active",
                "org_visible_principal_ids": [10],
                "pg_admin_username": "x",
                "pg_permissions": [],
                "pg_actions": {},
                "pg_capabilities_ok": False,
            },
            None,
        )
        self.assertFalse(staff_may_pg_page(staff, "pg_users"))
        self.assertFalse(staff_may_pg_action(staff, "users", "update"))

    def test_m_owner_capabilities_not_inherited(self) -> None:
        # Accidental is_owner PG role must not grant Owner/web Owner powers
        role = _role_matrix(
            name="Administrator",
            role_id=60,
            permissions={},
            is_owner=True,
        )
        staff = _level1_staff(10, parent_id=1, pg_username="m_pg", role=role)
        self.assertFalse(staff.get("pg_is_owner"))
        self.assertFalse(staff.get("web_owner"))
        self.assertEqual(staff.get("pg_permissions"), [])
        self.assertFalse(staff_may_pg_page(staff, "pg_admins"))
        self.assertFalse(staff_may_pg_page(staff, "pg_users"))

    def test_n_o_ui_flags_match_backend(self) -> None:
        role = _role_matrix(name="Mix", role_id=61, permissions=USERS_VIEW_ONLY)
        staff = _level1_staff(10, parent_id=1, pg_username="n_pg", role=role)
        flags = ui_pg_action_flags(staff)
        self.assertFalse(flags["users"]["update"])
        self.assertFalse(flags["users"]["create"])
        # Hidden UI action cannot be allowed by backend either (direct HTTP)
        self.assertEqual(
            flags["users"]["update"],
            staff_may_pg_action(staff, "users", "update"),
        )
        self.assertEqual(
            flags["users"]["update"],
            staff_pg_action(staff, "users", "update"),
        )


class Phase2CClientIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_principal_client_cache_isolated(self) -> None:
        from app.services import pasarguard as pg_mod
        from app.services.secret_box import encrypt_secret

        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            a = await create_principal(
                session,
                parent_id=owner.id,
                depth=1,
                pg_username="cache_a",
                pg_password_enc=encrypt_secret("AaBb12!@CdEf"),
            )
            b = await create_principal(
                session,
                parent_id=owner.id,
                depth=1,
                pg_username="cache_b",
                pg_password_enc=encrypt_secret("AaBb12!@CdEf"),
            )
            await session.commit()

            client_a = SimpleNamespace(_token="tok_a", username="cache_a")
            client_b = SimpleNamespace(_token="tok_b", username="cache_b")

            async def fake_ensure(self):
                return None

            with patch.object(
                pg_mod,
                "PasarGuardClient",
                side_effect=[client_a, client_b],
            ):
                # Manually inject ensure_token
                client_a.ensure_token = fake_ensure
                client_b.ensure_token = fake_ensure
                # Bypass real constructor by patching after — use direct cache test instead
                pg_mod._pg_principal_cache.clear()
                pg_mod._pg_principal_cache[(int(a.id), "cache_a")] = client_a
                pg_mod._pg_principal_cache[(int(b.id), "cache_b")] = client_b
                got_a = await pg_mod.get_pg_for_principal(session, principal_id=a.id)
                got_b = await pg_mod.get_pg_for_principal(session, principal_id=b.id)
            self.assertIs(got_a, client_a)
            self.assertIs(got_b, client_b)
            self.assertIsNot(got_a, got_b)
            # Owner platform cache is separate
            self.assertNotIn(("owner", "x"), pg_mod._pg_principal_cache)

    async def test_missing_password_enc_deny_client(self) -> None:
        from app.services.pasarguard import PasarGuardError, get_pg_for_principal

        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            a = await create_principal(
                session,
                parent_id=owner.id,
                depth=1,
                pg_username="nopwd",
                pg_password_enc=None,
            )
            await session.commit()
            with self.assertRaises(PasarGuardError):
                await get_pg_for_principal(session, principal_id=a.id)


class Phase2COwnerIsolationTests(unittest.TestCase):
    def test_owner_staff_not_affected_by_level1_clamp(self) -> None:
        owner_staff = {
            "role": "admin",
            "web_owner": True,
            "org_principal_id": 1,
            "org_depth": 0,
            "org_parent_id": None,
            "org_status": "active",
            "org_visible_principal_ids": [1, 10],
            "pg_is_owner": True,
            "pg_permissions": ["pg_admins", "pg_users", "pg_nodes"],
        }
        # Level-1 helper must not clamp Owner sessions
        from app.services.principal_pg_authz import is_level1_principal_staff

        self.assertFalse(is_level1_principal_staff(owner_staff))


if __name__ == "__main__":
    unittest.main()
