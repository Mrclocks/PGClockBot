"""Phase 3D — Level-2 PasarGuard permission parity (same engine as L1)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
import app.db.models  # noqa: F401
from app.services.authz import authz_from_staff, can_pg_page
from app.services.org_principals import create_principal, ensure_owner_principal
from app.services.org_scope import visible_principal_ids
from app.services.pg_access import (
    enrich_staff_pg_from_role,
    map_pg_role_to_features,
    staff_pg_action,
)
from app.services.pg_object_scope import pg_object_in_staff_scope
from app.services.pg_user_scope import pg_user_in_staff_scope
from app.services.principal_child_provisioning import (
    ChildProvisionError,
    Level2ProvisionRequest,
    provision_level2_child,
)
from app.services.principal_pg_authz import (
    apply_level1_pg_local_safety,
    apply_principal_pg_local_safety,
    authorize_pg_action,
    authorize_pg_page,
    is_level1_principal_staff,
    staff_may_pg_action,
    staff_may_pg_page,
    ui_pg_action_flags,
)
from app.services.principal_provisioning import (
    Level1ProvisionRequest,
    PrincipalProvisionError,
    provision_level1_principal,
)
from app.services.principal_web_identity import ROLE_PRINCIPAL
from app.services.resource_principal import (
    resolve_resource_principal,
    resource_in_principal_scope,
)
from app.services.secret_box import encrypt_secret


def _role_matrix(*, name: str, role_id: int, permissions: dict, is_owner: bool = False) -> dict:
    return {
        "id": role_id,
        "name": name,
        "is_owner": is_owner,
        "permissions": permissions,
    }


USERS_VIEW_ONLY = {
    "users": {"read": True, "read_simple": True, "create": False, "update": False, "delete": False},
}
USERS_VIEW_UPDATE = {
    "users": {"read": True, "update": True, "create": False, "delete": False},
}
NODES_VIEW_ONLY = {
    "nodes": {"read": True, "read_simple": True, "update": False, "create": False, "delete": False},
}
NODES_VIEW_UPDATE = {
    "nodes": {"read": True, "update": True, "create": False, "delete": False},
}
BROAD = {
    "users": {"read": True, "create": True, "update": True, "delete": True},
    "nodes": {"read": True, "update": True, "create": True, "delete": True, "reconnect": True},
    "templates": {"read": True, "update": True, "create": True, "delete": True},
    "groups": {"read": True, "update": True, "create": True, "delete": True},
    "hosts": {"read": True, "update": True, "create": True, "delete": True},
    "admins": {"read": True, "create": True, "update": True, "delete": True},
}


def _principal_staff(
    principal_id: int,
    *,
    parent_id: int,
    depth: int,
    pg_username: str,
    role: dict,
    visible: list[int] | None = None,
    credentials_ready: bool = True,
    capabilities_ok: bool = True,
) -> dict:
    features = map_pg_role_to_features(role)
    staff = {
        "role": ROLE_PRINCIPAL,
        "username": f"web_{pg_username}",
        "web_identity_id": principal_id,
        "pg_admin_username": pg_username,
        "pg_credentials_ready": credentials_ready,
        "pg_capabilities_ok": capabilities_ok,
        "org_principal_id": principal_id,
        "org_depth": depth,
        "org_parent_id": parent_id,
        "org_status": "active",
        "org_visible_principal_ids": visible or [principal_id],
        "web_owner": False,
        "pg_is_owner": False,
    }
    staff = enrich_staff_pg_from_role(staff, features, role)
    return apply_principal_pg_local_safety(staff, role)


def _l2_staff(principal_id: int, parent_id: int, pg_username: str, role: dict, **kw):
    return _principal_staff(
        principal_id, parent_id=parent_id, depth=2, pg_username=pg_username, role=role, **kw
    )


def _l1_staff(principal_id: int, parent_id: int, pg_username: str, role: dict, **kw):
    return _principal_staff(
        principal_id, parent_id=parent_id, depth=1, pg_username=pg_username, role=role, **kw
    )


def _http_node_update(staff: dict, node: dict) -> str:
    """Mirror POST /pg/nodes/{id}/edit gates (page + action + object scope)."""
    if not staff_may_pg_page(staff, "pg_nodes"):
        return "page_denied"
    if not staff_pg_action(staff, "nodes", "update"):
        return "capability_denied"
    if not pg_object_in_staff_scope(node, staff):
        return "scope_denied"
    return "ok"


def _http_user_update(staff: dict, user: dict) -> str:
    """Mirror POST user mutation: page + action + user object scope."""
    if not staff_may_pg_page(staff, "pg_users"):
        return "page_denied"
    if not staff_pg_action(staff, "users", "update"):
        return "capability_denied"
    if not pg_user_in_staff_scope(user, staff):
        return "scope_denied"
    return "ok"


class Phase3DLevel2ParityTests(unittest.TestCase):
    def test_same_engine_as_l1(self) -> None:
        self.assertIs(apply_principal_pg_local_safety, apply_level1_pg_local_safety)
        role = _role_matrix(name="Support", role_id=70, permissions=USERS_VIEW_ONLY)
        l1 = _l1_staff(10, 1, "l1_pg", role)
        l2 = _l2_staff(11, 10, "l2_pg", role)
        self.assertTrue(is_level1_principal_staff(l1))
        self.assertTrue(is_level1_principal_staff(l2))
        self.assertEqual(l1["pg_permissions"], l2["pg_permissions"])
        self.assertEqual(
            staff_may_pg_page(l1, "pg_users"),
            staff_may_pg_page(l2, "pg_users"),
        )

    def test_a_users_view_own(self) -> None:
        role = _role_matrix(name="Support", role_id=71, permissions=USERS_VIEW_ONLY)
        staff = _l2_staff(11, 10, "a1_pg", role)
        self.assertTrue(staff_may_pg_page(staff, "pg_users"))
        self.assertTrue(authorize_pg_page(staff, "pg_users").allowed)
        mine = {"id": 1, "admin": {"username": "a1_pg"}}
        self.assertTrue(pg_user_in_staff_scope(mine, staff))

    def test_b_without_users_view_deny(self) -> None:
        role = _role_matrix(name="NodeManager", role_id=72, permissions=NODES_VIEW_ONLY)
        staff = _l2_staff(11, 10, "a1_pg", role)
        self.assertFalse(staff_may_pg_page(staff, "pg_users"))
        self.assertFalse(authorize_pg_page(staff, "pg_users").allowed)

    def test_c_users_view_without_update(self) -> None:
        role = _role_matrix(name="Finance", role_id=73, permissions=USERS_VIEW_ONLY)
        staff = _l2_staff(11, 10, "a1_pg", role)
        self.assertTrue(staff_may_pg_page(staff, "pg_users"))
        self.assertFalse(staff_pg_action(staff, "users", "update"))
        self.assertFalse(staff_may_pg_action(staff, "users", "update"))
        flags = ui_pg_action_flags(staff)
        self.assertFalse(flags["users"]["update"])
        self.assertEqual(flags["users"]["update"], staff_may_pg_action(staff, "users", "update"))

    def test_d_nodes_view_without_update(self) -> None:
        role = _role_matrix(name="NodeManager", role_id=74, permissions=NODES_VIEW_ONLY)
        staff = _l2_staff(11, 10, "a1_pg", role)
        self.assertTrue(staff_may_pg_page(staff, "pg_nodes"))
        self.assertFalse(staff_pg_action(staff, "nodes", "update"))
        self.assertEqual(
            _http_node_update(staff, {"id": 9, "admin": {"username": "a1_pg"}}),
            "capability_denied",
        )

    def test_e_sibling_denied_despite_broad_pg(self) -> None:
        role = _role_matrix(name="CustomRole123", role_id=75, permissions=BROAD)
        a1 = _l2_staff(11, 10, "a1_pg", role)
        sibling_user = {"id": 2, "admin": {"username": "a2_pg"}}
        sibling_node = {"id": 3, "admin": {"username": "a2_pg"}}
        self.assertTrue(staff_may_pg_action(a1, "users", "update"))
        self.assertFalse(pg_user_in_staff_scope(sibling_user, a1))
        self.assertFalse(pg_object_in_staff_scope(sibling_node, a1))
        d = authorize_pg_action(a1, "users", "update", resource_principal_id=12)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "resource_out_of_scope")
        self.assertEqual(_http_user_update(a1, sibling_user), "scope_denied")
        self.assertEqual(_http_node_update(a1, sibling_node), "scope_denied")

    def test_f_parent_owned_resources_denied(self) -> None:
        role = _role_matrix(name="Support", role_id=76, permissions=BROAD)
        a1 = _l2_staff(11, 10, "a1_pg", role, visible=[11])
        parent_res = resolve_resource_principal(SimpleNamespace(owner_principal_id=10))
        vis = frozenset(int(x) for x in a1["org_visible_principal_ids"])
        self.assertFalse(
            resource_in_principal_scope(
                actor_visible_principal_ids=vis, resolution=parent_res
            )
        )
        parent_user = {"id": 8, "admin": {"username": "l1_a"}}
        self.assertFalse(pg_user_in_staff_scope(parent_user, a1))

    def test_g_owner_platform_denied(self) -> None:
        role = _role_matrix(name="Administrator", role_id=77, permissions=BROAD)
        a1 = _l2_staff(11, 10, "a1_pg", role)
        self.assertFalse(staff_may_pg_page(a1, "pg_admins"))
        self.assertFalse(staff_may_pg_page(a1, "backup"))
        self.assertFalse(staff_pg_action(a1, "admins", "create"))
        self.assertFalse(bool(a1.get("pg_is_owner")))
        self.assertFalse(bool(a1.get("web_owner")))
        owner_user = {"id": 1, "admin": {"username": "env_owner_pg"}}
        self.assertFalse(pg_user_in_staff_scope(owner_user, a1))

    def test_h_cannot_use_parent_capabilities(self) -> None:
        parent_role = _role_matrix(name="Operator", role_id=78, permissions=BROAD)
        child_role = _role_matrix(name="Support", role_id=79, permissions=USERS_VIEW_ONLY)
        parent = _l1_staff(10, 1, "l1_a", parent_role, visible=[10, 11, 12])
        child = _l2_staff(11, 10, "a1_pg", child_role)
        self.assertTrue(staff_may_pg_page(parent, "pg_nodes"))
        self.assertFalse(staff_may_pg_page(child, "pg_nodes"))
        self.assertNotEqual(parent["pg_permissions"], child["pg_permissions"])
        self.assertNotEqual(parent.get("pg_admin_username"), child.get("pg_admin_username"))

    def test_i_cannot_use_owner_capabilities(self) -> None:
        owner_equiv = _role_matrix(
            name="Owner", role_id=1, permissions={}, is_owner=True
        )
        staff = _l2_staff(11, 10, "a1_pg", owner_equiv)
        self.assertFalse(staff.get("pg_is_owner"))
        self.assertEqual(staff.get("pg_permissions"), [])
        self.assertFalse(staff_may_pg_page(staff, "pg_admins"))
        self.assertFalse(staff_may_pg_page(staff, "pg_users"))

    def test_j_same_role_isolated_scopes(self) -> None:
        role = _role_matrix(name="Shared", role_id=80, permissions=BROAD)
        a1 = _l2_staff(11, 10, "a1_pg", role)
        a2 = _l2_staff(12, 10, "a2_pg", role)
        self.assertEqual(a1["pg_permissions"], a2["pg_permissions"])
        self.assertTrue(staff_may_pg_action(a1, "nodes", "update"))
        self.assertTrue(staff_may_pg_action(a2, "nodes", "update"))
        self.assertEqual(a1["org_visible_principal_ids"], [11])
        self.assertEqual(a2["org_visible_principal_ids"], [12])
        self.assertFalse(
            authorize_pg_action(a1, "nodes", "update", resource_principal_id=12).allowed
        )
        self.assertEqual(
            _http_node_update(a1, {"id": 5, "admin": {"username": "a2_pg"}}),
            "scope_denied",
        )

    def test_k_different_roles_differ(self) -> None:
        view = _role_matrix(name="Support", role_id=81, permissions=USERS_VIEW_ONLY)
        nodes = _role_matrix(name="NodeManager", role_id=82, permissions=NODES_VIEW_UPDATE)
        a1 = _l2_staff(11, 10, "a1_pg", view)
        a2 = _l2_staff(12, 10, "a2_pg", nodes)
        self.assertTrue(staff_may_pg_page(a1, "pg_users"))
        self.assertFalse(staff_may_pg_page(a1, "pg_nodes"))
        self.assertTrue(staff_may_pg_page(a2, "pg_nodes"))
        self.assertFalse(staff_may_pg_page(a2, "pg_users"))

    def test_l_arbitrary_role_names(self) -> None:
        for name in ("Support", "Finance", "NodeManager", "CustomRole123", "RoleX"):
            role = _role_matrix(name=name, role_id=90, permissions=USERS_VIEW_ONLY)
            staff = _l2_staff(11, 10, "a1_pg", role)
            self.assertTrue(staff_may_pg_page(staff, "pg_users"), msg=name)
            self.assertFalse(staff_may_pg_page(staff, "pg_nodes"), msg=name)
            self.assertEqual(int(staff["org_depth"]), 2)

    def test_m_missing_pg_identity_deny(self) -> None:
        staff = apply_principal_pg_local_safety(
            {
                "role": ROLE_PRINCIPAL,
                "org_principal_id": 11,
                "org_depth": 2,
                "org_parent_id": 10,
                "org_status": "active",
                "org_visible_principal_ids": [11],
                "pg_admin_username": None,
                "pg_permissions": ["pg_users"],  # stuffed — must not count
                "pg_credentials_ready": False,
            },
            None,
        )
        self.assertFalse(staff_may_pg_page(staff, "pg_users"))
        self.assertEqual(authorize_pg_page(staff, "pg_users").reason, "pg_capabilities_unavailable")

    def test_n_missing_pg_role_deny(self) -> None:
        staff = apply_principal_pg_local_safety(
            {
                "role": ROLE_PRINCIPAL,
                "org_principal_id": 11,
                "org_depth": 2,
                "org_parent_id": 10,
                "org_status": "active",
                "org_visible_principal_ids": [11],
                "pg_admin_username": "a1_pg",
                "pg_credentials_ready": True,
                "pg_permissions": [],
                "pg_actions": {},
                "pg_capabilities_ok": False,
            },
            None,
        )
        self.assertFalse(staff_may_pg_page(staff, "pg_users"))
        self.assertFalse(staff_pg_action(staff, "users", "update"))

    def test_o_pg_unavailable_deny(self) -> None:
        staff = _l2_staff(
            11,
            10,
            "a1_pg",
            _role_matrix(name="Support", role_id=91, permissions=USERS_VIEW_ONLY),
            capabilities_ok=False,
        )
        # capabilities_ok False wins even if matrix was mapped then marked unavailable
        staff["pg_capabilities_ok"] = False
        self.assertFalse(staff_may_pg_page(staff, "pg_users"))
        self.assertFalse(staff_may_pg_action(staff, "users", "update"))

    def test_p_http_mutation_cannot_bypass_capability(self) -> None:
        role = _role_matrix(name="Support", role_id=92, permissions=NODES_VIEW_ONLY)
        staff = _l2_staff(11, 10, "a1_pg", role)
        own = {"id": 4, "admin": {"username": "a1_pg"}}
        self.assertEqual(_http_node_update(staff, own), "capability_denied")
        self.assertEqual(
            ui_pg_action_flags(staff)["nodes"]["update"],
            staff_may_pg_action(staff, "nodes", "update"),
        )

    def test_q_http_mutation_cannot_bypass_scope(self) -> None:
        role = _role_matrix(name="NodeManager", role_id=93, permissions=NODES_VIEW_UPDATE)
        staff = _l2_staff(11, 10, "a1_pg", role)
        self.assertEqual(
            _http_node_update(staff, {"id": 4, "admin": {"username": "a1_pg"}}),
            "ok",
        )
        self.assertEqual(
            _http_node_update(staff, {"id": 5, "admin": {"username": "a2_pg"}}),
            "scope_denied",
        )

    def test_s_l1_authorization_unchanged(self) -> None:
        role = _role_matrix(name="CustomView", role_id=50, permissions=USERS_VIEW_ONLY)
        staff = _l1_staff(10, 1, "a_pg", role)
        self.assertTrue(staff_may_pg_page(staff, "pg_users"))
        self.assertFalse(staff_may_pg_action(staff, "users", "update"))
        self.assertFalse(staff_may_pg_page(staff, "pg_admins"))
        self.assertTrue(can_pg_page(authz_from_staff(staff), "pg_users"))


class Phase3DProvisionAndCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_r_l2_cannot_create_any_principal(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            a = await create_principal(session, parent_id=int(owner.id), depth=1)
            a1 = await create_principal(session, parent_id=int(a.id), depth=2)
            await session.commit()
            vis = await visible_principal_ids(session, a1)
            role = _role_matrix(name="Administrator", role_id=11, permissions=BROAD)
            staff = _l2_staff(
                int(a1.id), int(a.id), "a1_pg", role, visible=sorted(vis)
            )
            staff["pg_can_create_admin"] = True
            staff["pg_actions"] = {"admins": {"create": True}}
            with self.assertRaises(ChildProvisionError) as ctx:
                await provision_level2_child(
                    session,
                    staff,
                    Level2ProvisionRequest(
                        pg_username="evil_l3",
                        pg_password="AaBb12!@CdEf",
                        idempotency_key="no-l3",
                    ),
                )
            self.assertEqual(ctx.exception.code, "not_level1")
            with self.assertRaises(PrincipalProvisionError) as ctx2:
                await provision_level1_principal(
                    session,
                    staff,
                    Level1ProvisionRequest(
                        pg_username="evil_l1",
                        pg_password="AaBb12!@CdEf",
                        idempotency_key="no-l1",
                    ),
                )
            self.assertEqual(ctx2.exception.code, "not_owner")
            self.assertFalse(staff_pg_action(staff, "admins", "create"))
            self.assertFalse(staff_pg_action(staff, "principals", "create"))

    async def test_cache_isolation_l2_vs_parent_sibling(self) -> None:
        from app.services import pasarguard as pg_mod

        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            a = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="l1_a",
                pg_password_enc=encrypt_secret("AaBb12!@CdEf"),
            )
            a1 = await create_principal(
                session,
                parent_id=int(a.id),
                depth=2,
                pg_username="l2_a1",
                pg_password_enc=encrypt_secret("CcDd34!@EfGh"),
            )
            a2 = await create_principal(
                session,
                parent_id=int(a.id),
                depth=2,
                pg_username="l2_a2",
                pg_password_enc=encrypt_secret("EeFf56!@GhIj"),
            )
            await session.commit()
            pg_mod._pg_principal_cache.clear()
            captured: list[str] = []

            class _Client:
                def __init__(self, *, username=None, password=None, access_token=None):
                    captured.append(username or "")
                    self.username = username
                    self._token = "t"
                    self._login_password = password

                async def ensure_token(self):
                    self._login_password = None
                    return self._token

            with patch.object(pg_mod, "PasarGuardClient", _Client):
                c1 = await pg_mod.get_pg_for_principal(session, principal_id=int(a1.id))
                c2 = await pg_mod.get_pg_for_principal(session, principal_id=int(a2.id))
                cp = await pg_mod.get_pg_for_principal(session, principal_id=int(a.id))
            self.assertEqual(c1.username, "l2_a1")
            self.assertEqual(c2.username, "l2_a2")
            self.assertEqual(cp.username, "l1_a")
            self.assertIsNot(c1, c2)
            self.assertIsNot(c1, cp)
            self.assertIn((int(a1.id), "l2_a1"), pg_mod._pg_principal_cache)
            self.assertNotIn((int(a1.id), "l1_a"), pg_mod._pg_principal_cache)

    async def test_role_cache_is_partitioned_by_client_identity(self) -> None:
        from app.services import pg_access as pg_acc
        from app.services.pg_read_cache import (
            ROLE_FEATURES_PATH,
            cache_put,
            clear_store,
            invalidate_capability_cache,
        )

        clear_store()
        role = {"id": 80, "name": "Shared", "is_owner": False, "permissions": USERS_VIEW_ONLY}
        # Poison only shop A's identity; shop B must still probe (fail closed → empty).
        cache_put(
            "u:shop_a@https://pg.example:role",
            ROLE_FEATURES_PATH,
            {"role_id": 80},
            {"ok": True, "features": ["pg_users", "pg_overview", "pg_nodes"], "role": role},
        )
        client_a = AsyncMock()
        client_a.base_url = "https://pg.example"
        client_a._login_username = "shop_a"
        client_a._read_cache_ident = lambda: "u:shop_a@https://pg.example:role"
        client_a.get_admin_role = AsyncMock(return_value=role)
        features_a, _ = await pg_acc.resolve_reseller_pg_features(80, client=client_a)
        self.assertIn("pg_users", features_a)
        client_a.get_admin_role.assert_not_awaited()

        client_b = AsyncMock()
        client_b.base_url = "https://pg.example"
        client_b._login_username = "shop_b"
        client_b._read_cache_ident = lambda: "u:shop_b@https://pg.example:role"
        client_b.get_admin_role = AsyncMock(side_effect=RuntimeError("no share"))
        client_b.get_admin_roles = AsyncMock(side_effect=RuntimeError("no share"))
        features_b, role_b = await pg_acc.resolve_reseller_pg_features(80, client=client_b)
        self.assertEqual(features_b, [])
        self.assertIsNone(role_b)
        a1 = _l2_staff(11, 10, "a1_pg", role)
        a2 = _l2_staff(12, 10, "a2_pg", role)
        self.assertEqual(a1["pg_permissions"], a2["pg_permissions"])
        self.assertNotEqual(a1["org_visible_principal_ids"], a2["org_visible_principal_ids"])
        invalidate_capability_cache()


if __name__ == "__main__":
    unittest.main()
