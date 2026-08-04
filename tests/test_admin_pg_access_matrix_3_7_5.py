"""Access matrix: restricted admins get PG panel only for granted features + ready client."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


def _role(*, users=False, nodes=False, system=False, hosts=False, owner=False):
    if owner:
        return {"is_owner": True, "permissions": {}}
    perms: dict = {}
    if users:
        perms["users"] = {"read": True}
    if nodes:
        perms["nodes"] = {"read": True}
    if system:
        perms["system"] = {"read": True}
    if hosts:
        perms["hosts"] = {"read": True}
    return {"is_owner": False, "permissions": perms}


class FeaturePermissionMatrixTests(unittest.TestCase):
    def test_users_only_no_nodes(self):
        from app.services.pg_access import map_pg_role_to_features

        feats = map_pg_role_to_features(_role(users=True, system=True))
        self.assertIn("pg_users", feats)
        self.assertIn("pg_overview", feats)
        self.assertNotIn("pg_nodes", feats)
        self.assertNotIn("pg_hosts", feats)

    def test_nodes_granted(self):
        from app.services.pg_access import map_pg_role_to_features

        feats = map_pg_role_to_features(_role(users=True, nodes=True))
        self.assertIn("pg_nodes", feats)
        self.assertIn("pg_users", feats)

    def test_empty_role_no_features(self):
        from app.services.pg_access import map_pg_role_to_features

        self.assertEqual(map_pg_role_to_features(_role()), [])
        self.assertEqual(map_pg_role_to_features(None), [])

    def test_owner_role_gets_all_features(self):
        from app.services.pg_access import PG_FEATURE_KEYS, map_pg_role_to_features

        feats = map_pg_role_to_features(_role(owner=True))
        self.assertEqual(feats, list(PG_FEATURE_KEYS))


class RequirePgPermGateTests(unittest.IsolatedAsyncioTestCase):
    """Simulate require_pg_perm decisions without spinning up FastAPI."""

    def _decide(self, staff: dict, perm: str) -> str:
        """Return 'allow' | 'deny_feature' | 'need_credentials' mirroring require_pg_perm."""
        if staff.get("role") == "admin":
            return "allow"
        features = staff.get("pg_permissions") or []
        if perm not in features:
            return "deny_feature"
        if not staff.get("pg_client_ready"):
            return "need_credentials"
        return "allow"

    def test_platform_admin_always_allowed(self):
        self.assertEqual(self._decide({"role": "admin"}, "pg_nodes"), "allow")

    def test_restricted_without_node_denied(self):
        staff = {
            "role": "pg_staff",
            "pg_permissions": ["pg_users", "pg_overview"],
            "pg_client_ready": True,
        }
        self.assertEqual(self._decide(staff, "pg_nodes"), "deny_feature")
        self.assertEqual(self._decide(staff, "pg_users"), "allow")

    def test_restricted_with_node_allowed_when_ready(self):
        staff = {
            "role": "pg_staff",
            "pg_permissions": ["pg_users", "pg_nodes"],
            "pg_client_ready": True,
        }
        self.assertEqual(self._decide(staff, "pg_nodes"), "allow")

    def test_feature_without_credentials_blocked(self):
        staff = {
            "role": "reseller",
            "pg_permissions": ["pg_users", "pg_nodes", "pg_overview"],
            "pg_client_ready": False,
        }
        self.assertEqual(self._decide(staff, "pg_users"), "need_credentials")
        self.assertEqual(self._decide(staff, "pg_overview"), "need_credentials")


class ReadyAdminPanelDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_reseller_overview_loads_own_admin(self):
        from app.services.pg_overview import build_reseller_pg_overview

        pg = MagicMock()
        pg.get_admin = AsyncMock(
            return_value={
                "username": "shop1",
                "total_users": 5,
                "used_traffic": 1024,
                "status": "active",
            }
        )
        pg.get_current_admin = AsyncMock()
        pg.get_users = AsyncMock(return_value={"users": []})
        with patch(
            "app.services.pasarguard.get_pg_for_staff",
            new=AsyncMock(return_value=(pg, False)),
        ):
            out = await build_reseller_pg_overview(
                {
                    "role": "reseller",
                    "bot_user_id": 11,
                    "pg_admin_username": "shop1",
                    "pg_permissions": ["pg_overview", "pg_users"],
                    "pg_client_ready": True,
                },
                session=AsyncMock(),
            )
        self.assertTrue(out["ready"], out.get("error"))
        self.assertIsNone(out.get("error"))
        self.assertEqual(out["username"], "shop1")

    async def test_list_pg_none_without_credentials(self):
        from app.api.pg_pages import _list_pg

        # Ready flag false → fail closed without touching Owner / client factories.
        with patch("app.api.pg_pages.get_pg", side_effect=AssertionError("Owner")):
            out = await _list_pg(
                AsyncMock(),
                {"role": "pg_staff", "pg_admin_username": "a1", "pg_client_ready": False},
            )
        self.assertIsNone(out)

    async def test_list_pg_staff_client_when_ready(self):
        from app.api.pg_pages import _list_pg

        client = object()
        with patch(
            "app.services.authz.resolve_pg_client",
            new=AsyncMock(return_value=(client, False)),
        ):
            out = await _list_pg(
                AsyncMock(),
                {
                    "role": "pg_staff",
                    "pg_admin_username": "a1",
                    "pg_client_ready": True,
                    "pg_permissions": ["pg_users"],
                },
            )
        self.assertIs(out, client)

    async def test_get_pg_for_staff_never_owner_for_pg_staff(self):
        from app.services.pasarguard import PasarGuardError, get_pg_for_staff

        with (
            patch("app.services.pasarguard.get_pg", side_effect=AssertionError("Owner")),
            patch(
                "app.services.pasarguard.get_pg_for_staff_admin",
                new=AsyncMock(side_effect=PasarGuardError("no pwd")),
            ),
        ):
            with self.assertRaises(PasarGuardError):
                await get_pg_for_staff(
                    AsyncMock(),
                    {"role": "pg_staff", "pg_admin_username": "lim1"},
                )


class SidebarGatingTests(unittest.TestCase):
    def test_nav_requires_feature_and_ready(self):
        src = (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8")
        self.assertIn("pg_client_ready", src)
        self.assertIn("has_pg_features and pg_client_ready", src)
        self.assertIn("pg_needs_credentials", src)
        # nodes gated by permission key
        self.assertIn("'pg_nodes' in pg_nav", src)
        self.assertIn("'pg_users' in pg_nav", src)


class EnrichStaffFromRoleTests(unittest.TestCase):
    def test_enrich_sets_permissions_from_features(self):
        from app.services.pg_access import enrich_staff_pg_from_role, map_pg_role_to_features

        role = _role(users=True, nodes=True, system=True)
        feats = map_pg_role_to_features(role)
        staff = enrich_staff_pg_from_role({"role": "pg_staff"}, feats, role)
        self.assertEqual(staff["pg_permissions"], feats)
        self.assertIn("pg_nodes", staff["pg_permissions"])
        self.assertIn("pg_users", staff["pg_permissions"])


if __name__ == "__main__":
    unittest.main()
