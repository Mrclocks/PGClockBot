"""Live PG menus follow GET /api/admin nested role, like quota boxes."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services.pg_access import (
    acl_from_admin_payload,
    clear_platform_pg_capability_cache,
)


ROOT = Path(__file__).resolve().parents[1]


def _role(*, nodes: bool, users: bool = True) -> dict:
    return {
        "id": 4,
        "name": "ops",
        "is_owner": False,
        "permissions": {
            "users": {"read": True, "create": True} if users else {"read": False},
            "nodes": {"read": True, "read_simple": True} if nodes else {"read": False},
            "hosts": {"read": False},
        },
    }


class AclFromAdminPayloadTests(unittest.TestCase):
    def test_nested_role_without_nodes_omits_menu(self):
        feats, role, owner = acl_from_admin_payload(
            {"username": "lim", "is_sudo": True, "role": _role(nodes=False)}
        )
        self.assertFalse(owner)
        self.assertNotIn("pg_nodes", feats)
        self.assertIn("pg_users", feats)
        self.assertEqual(role["id"], 4)

    def test_true_owner_role_keeps_nodes(self):
        feats, _role_out, owner = acl_from_admin_payload(
            {"username": "root", "role": {"id": 1, "is_owner": True, "permissions": {}}}
        )
        self.assertTrue(owner)
        self.assertIn("pg_nodes", feats)
        self.assertIn("pg_admins", feats)

    def test_legacy_sudo_without_role_is_owner(self):
        feats, _role_out, owner = acl_from_admin_payload({"username": "root", "is_sudo": True})
        self.assertTrue(owner)
        self.assertIn("pg_nodes", feats)


class NestedRoleBeatsRoleCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clear_platform_pg_capability_cache()

    async def asyncTearDown(self):
        clear_platform_pg_capability_cache()

    async def test_revoked_nodes_on_nested_role_not_restored_by_cached_role(self):
        from app.services.pg_access import resolve_platform_pg_capabilities

        stale = _role(nodes=True)
        live = _role(nodes=False)
        admin = {"username": "lim", "role": live, "is_sudo": False}
        with patch("app.services.pasarguard.get_pg") as gp:
            client = AsyncMock()
            client.ensure_token = AsyncMock(return_value="tok")
            client.get_current_admin = AsyncMock(return_value=admin)
            client.get_admin_role = AsyncMock(return_value=stale)
            gp.return_value = client
            with patch("app.config.get_settings") as gs:
                gs.return_value.pg_username = "lim"
                gs.return_value.pg_password = "x"
                gs.return_value.pg_base_url = "https://pg.example"
                caps = await resolve_platform_pg_capabilities(use_cache=False)
        self.assertTrue(caps["ok"])
        self.assertNotIn("pg_nodes", caps["features"])
        self.assertIn("pg_users", caps["features"])
        client.get_admin_role.assert_not_called()


class SurfaceContractTests(unittest.TestCase):
    def test_sidebar_and_tabs_use_pg_permissions_only(self):
        from tests.sidebar_view import labels, sidebar

        self.assertNotIn("pg", sidebar("admin"))
        self.assertEqual(labels(sidebar("admin", pg=frozenset({"pg_nodes"}))["pg"]), ["نودها"])
        macros = (ROOT / "app/web/templates/macros.html").read_text(encoding="utf-8")
        self.assertNotIn("admin or 'pg_nodes'", macros)
        self.assertIn("'pg_nodes' in perms", macros)
        self.assertNotIn("staff.role == 'admin'", macros[macros.find("macro pg_tabs") :])

    def test_dashboard_hides_node_conn_without_permission(self):
        home = (ROOT / "app/web/templates/_home_dash_body.html").read_text(encoding="utf-8")
        self.assertIn("show_pg_nodes", home)
        tiles = home[home.find("status_tiles(tiles)") - 1500 : home.find("status_tiles(tiles)")]
        self.assertIn("show_pg_nodes", tiles)
        self.assertIn("'/pg/nodes'", tiles)

    def test_home_bundle_skips_nodes_when_disabled(self):
        src = (ROOT / "app/services/home_overview.py").read_text(encoding="utf-8")
        self.assertIn("include_nodes", src)
        self.assertIn("if include_nodes", src)
        pages = (ROOT / "app/api/home_pages.py").read_text(encoding="utf-8")
        self.assertIn("include_nodes=show_pg_nodes", pages)


class PgHomeBundleSkipTests(unittest.IsolatedAsyncioTestCase):
    async def test_include_nodes_false_does_not_call_nodes_api(self):
        from app.services.home_overview import pg_home_bundle

        pg = AsyncMock()
        pg.ensure_token = AsyncMock(return_value="tok")
        pg.get_admins_simple = AsyncMock(return_value=[])
        pg.get_groups_simple = AsyncMock(return_value=[])
        pg.get_hosts = AsyncMock(return_value=[])
        pg.get_nodes_simple = AsyncMock(return_value=[{"id": 1}])
        pg.get_system_stats = AsyncMock(return_value={"version": "1"})
        with patch("app.services.home_overview.get_pg", return_value=pg):
            summary, nodes = await pg_home_bundle(include_nodes=False)
        self.assertTrue(summary["ok"])
        pg.get_nodes_simple.assert_not_called()
        self.assertEqual(nodes["overall"], "neutral")
        self.assertFalse(nodes["total"])


if __name__ == "__main__":
    unittest.main()
