"""3.8.0 — Unified permission architecture regression suite.

Covers the permanent authz model:
  Identity → Role → Permission → PG Credential → PG Client → Data Scope → Response

Surfaces: Owner, Admin, Sub Admin (pg_staff), Reseller, legacy vs new accounts,
Dashboard, Users/Hosts/Nodes/Groups/Templates/Plans, Telegram Bot, Web Panel,
Backend API, permission/role/credential sync, cross-tenant isolation.

Security invariants from 3.7.2–3.7.5 MUST remain (no Owner fallback).
"""

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


class Version380Tests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertGreaterEqual(tuple(int(x) for x in __version__.split(".")), (3, 8, 0))
        notes = (ROOT / "app/services/release_notes.py").read_text(encoding="utf-8")
        self.assertIn('"3.8.0"', notes)
        self.assertIn("authz", notes)


class AuthzSingleSourceOfTruthTests(unittest.TestCase):
    def test_module_exists_and_exports_pipeline(self):
        from app.services import authz

        for name in (
            "decide_pg",
            "can_pg",
            "can_shop",
            "client_ready",
            "nav_context",
            "resolve_pg_client",
            "enrich_reseller_actor",
            "enrich_pg_staff_actor",
            "first_allowed_pg_path",
            "credentials_required_url",
            "assert_staff_pg_session",
            "bot_pg_feature_entries",
        ):
            self.assertTrue(hasattr(authz, name), msg=name)

    def test_owner_always_allowed(self):
        from app.services.authz import PgDecision, decide_pg, can_shop, nav_context

        admin = {"role": "admin"}
        self.assertEqual(decide_pg(admin, "pg_nodes"), PgDecision.ALLOW)
        self.assertTrue(can_shop(admin, "plans"))
        nav = nav_context(admin)
        self.assertTrue(nav["is_admin"])
        self.assertTrue(nav["has_pg"])
        self.assertFalse(nav["pg_needs_credentials"])

    def test_pg_staff_feature_matrix(self):
        from app.services.authz import PgDecision, decide_pg, nav_context

        staff = {
            "role": "pg_staff",
            "pg_permissions": ["pg_overview", "pg_users"],
            "pg_client_ready": True,
        }
        self.assertEqual(decide_pg(staff, "pg_users"), PgDecision.ALLOW)
        self.assertEqual(decide_pg(staff, "pg_nodes"), PgDecision.DENY_FEATURE)
        nav = nav_context(staff)
        self.assertIn("pg_users", nav["pg_visible"])
        self.assertNotIn("pg_nodes", nav["pg_visible"])

    def test_sub_admin_without_credentials(self):
        from app.services.authz import PgDecision, decide_pg, nav_context

        staff = {
            "role": "pg_staff",
            "pg_permissions": ["pg_users", "pg_nodes", "pg_overview"],
            "pg_client_ready": False,
        }
        self.assertEqual(decide_pg(staff, "pg_users"), PgDecision.NEED_CREDENTIALS)
        nav = nav_context(staff)
        self.assertTrue(nav["pg_needs_credentials"])
        self.assertFalse(nav["has_pg"])
        self.assertEqual(nav["pg_visible"], [])

    def test_reseller_shop_and_pg_layers(self):
        from app.services.authz import can_shop, decide_pg, PgDecision

        reseller = {
            "role": "reseller",
            "bot_user_id": 9,
            "permissions": ["dashboard", "plans"],
            "pg_permissions": ["pg_users"],
            "pg_client_ready": True,
        }
        self.assertTrue(can_shop(reseller, "plans"))
        self.assertFalse(can_shop(reseller, "payments"))
        self.assertEqual(decide_pg(reseller, "pg_users"), PgDecision.ALLOW)
        self.assertEqual(decide_pg(reseller, "pg_hosts"), PgDecision.DENY_FEATURE)

    def test_pg_staff_has_no_shop(self):
        from app.services.authz import can_shop

        self.assertFalse(can_shop({"role": "pg_staff", "permissions": ["plans"]}, "plans"))

    def test_legacy_and_new_nav_identical_for_same_acl(self):
        from app.services.authz import nav_context

        legacy = {
            "role": "reseller",
            "permissions": ["dashboard", "orders"],
            "pg_permissions": ["pg_overview", "pg_users"],
            "pg_client_ready": False,  # legacy missing enc
        }
        repaired = dict(legacy)
        repaired["pg_client_ready"] = True  # after credential sync
        n1, n2 = nav_context(legacy), nav_context(repaired)
        self.assertEqual(n1["pg_permissions"], n2["pg_permissions"])
        self.assertTrue(n1["pg_needs_credentials"])
        self.assertFalse(n2["pg_needs_credentials"])
        self.assertTrue(n2["has_pg"])


class AuthzWebBotApiParityTests(unittest.TestCase):
    """Web require_pg_perm, bot submenu, and authz.decide_pg must agree."""

    def test_bot_pg_entries_match_web_features_for_limited(self):
        from app.services.authz import bot_pg_feature_entries, can_pg

        actor = {
            "role": "pg_staff",
            "pg_permissions": ["pg_users", "pg_nodes"],
            "pg_client_ready": True,
        }
        entries = bot_pg_feature_entries(actor)
        keys = {k for k, _ in entries}
        self.assertIn("pg_users", keys)
        self.assertIn("pg_nodes", keys)
        self.assertNotIn("pg_stats", keys)  # needs pg_overview
        self.assertTrue(can_pg(actor, "pg_users"))
        self.assertFalse(can_pg(actor, "pg_overview"))

    def test_bot_platform_admin_full_submenu(self):
        from app.services.authz import bot_pg_feature_entries

        entries = bot_pg_feature_entries({"role": "admin"})
        keys = {k for k, _ in entries}
        self.assertEqual(
            keys,
            {"pg_stats", "pg_users", "pg_create", "pg_search", "pg_nodes", "pg_group", "pg_template"},
        )

    def test_keyboards_delegate_to_authz(self):
        src = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")
        self.assertIn("bot_pg_feature_entries", src)
        self.assertIn("can_shop_profile", src)

    def test_require_pg_perm_uses_decide_pg(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("decide_pg", src)
        self.assertIn("credentials_required_url", src)
        self.assertIn("enrich_reseller_actor", src)
        self.assertIn("enrich_pg_staff_actor", src)


class OwnerFallbackNeverReturnsTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_pg_client_requires_session_for_limited(self):
        from app.services.authz import assert_staff_pg_session
        from app.services.pasarguard import PasarGuardError

        with self.assertRaises(PasarGuardError):
            assert_staff_pg_session(
                {"role": "reseller", "pg_client_ready": True, "bot_user_id": 1},
                None,
            )

    async def test_assert_owned_user_never_owner_without_session(self):
        from app.api.pg_pages import _assert_owned_user

        with patch("app.api.pg_pages.get_pg", side_effect=AssertionError("Owner")):
            out = await _assert_owned_user(
                {"role": "pg_staff", "pg_admin_username": "lim", "pg_client_ready": True},
                1,
                session=None,
            )
        self.assertIsNone(out)

    async def test_get_pg_for_staff_still_fail_closed(self):
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

    async def test_list_pg_none_without_ready(self):
        from app.api.pg_pages import _list_pg

        with patch("app.api.pg_pages.get_pg", side_effect=AssertionError("Owner")):
            out = await _list_pg(
                AsyncMock(),
                {
                    "role": "reseller",
                    "bot_user_id": 3,
                    "pg_admin_username": "shop",
                    "pg_client_ready": False,
                },
            )
        self.assertIsNone(out)

    def test_delete_bot_user_no_owner_fallback_on_reseller_fail(self):
        src = (ROOT / "app/services/users.py").read_text(encoding="utf-8")
        start = src.index("async def delete_bot_user")
        body = src[start : start + 5000]
        self.assertIn("Never fall back to Owner", body)
        self.assertNotIn("pg_client = get_pg()", body.split("if reseller_id:")[1][:400])


class CredentialSyncDeterminismTests(unittest.TestCase):
    def test_owner_password_sync_helper_exists(self):
        src = (ROOT / "app/api/security.py").read_text(encoding="utf-8")
        self.assertIn("_sync_owner_pg_password", src)
        self.assertIn("PG_PASSWORD", src)

    def test_enrich_sets_ready_from_enc(self):
        from app.services.authz import enrich_actor_pg_acl

        staff = enrich_actor_pg_acl(
            {"role": "pg_staff"},
            features=["pg_users"],
            role=_role(users=True),
            pg_client_ready=True,
            pg_admin_username="a1",
            pg_role_id=2,
        )
        self.assertTrue(staff["pg_client_ready"])
        self.assertEqual(staff["pg_permissions"], ["pg_users"])
        staff2 = enrich_actor_pg_acl(
            {"role": "reseller"},
            features=["pg_users"],
            role=_role(users=True),
            pg_client_ready=False,
            pg_admin_username="shop",
        )
        self.assertFalse(staff2["pg_client_ready"])


class DashboardDetailConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_overview_and_list_share_get_pg_for_staff(self):
        from app.services.pg_overview import build_reseller_pg_overview

        pg = MagicMock()
        pg.get_admin = AsyncMock(
            return_value={"username": "shop1", "total_users": 2, "used_traffic": 0}
        )
        pg.get_current_admin = AsyncMock()
        pg.get_users = AsyncMock(return_value={"users": []})
        with patch(
            "app.services.pasarguard.get_pg_for_staff",
            new=AsyncMock(return_value=(pg, False)),
        ) as mock_staff:
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
        self.assertTrue(out["ready"])
        mock_staff.assert_awaited()

    def test_pages_redirect_not_empty_on_missing_creds(self):
        src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn("_credentials_redirect", src)
        self.assertGreaterEqual(src.count("return _credentials_redirect()"), 6)

    def test_base_html_uses_authz_nav(self):
        src = (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8")
        self.assertIn("authz_nav", src)
        self.assertIn("pg_needs_credentials", src)


class CrossTenantIsolationTests(unittest.TestCase):
    def test_shop_scope_fail_closed(self):
        from app.services.shop_scope import shop_owner_id, is_platform_admin

        self.assertIsNone(shop_owner_id({"role": "pg_staff", "pg_admin_username": "x"}))
        self.assertIsNone(shop_owner_id({"role": "reseller"}))
        self.assertEqual(shop_owner_id({"role": "reseller", "bot_user_id": 7}), 7)
        self.assertTrue(is_platform_admin({"role": "admin"}))

    def test_plans_catalog_limited_requires_session(self):
        src = (ROOT / "app/services/plans_catalog.py").read_text(encoding="utf-8")
        self.assertIn("is_limited_pg_actor", src)
        self.assertIn("resolve_pg_client", src)


class RoleAndPermissionChangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_enrich_reseller_clears_stale_cookie_acl(self):
        from app.services.authz import enrich_reseller_actor
        from app.services.secret_box import encrypt_secret

        profile = SimpleNamespace(
            user_id=5,
            web_username="shop",
            web_permissions="dashboard,plans",
            pg_admin_username="shop_pg",
            pg_role_id=3,
            pg_admin_password_enc=encrypt_secret("Aa1!bbbbbbbb"),
        )
        stale = {
            "role": "reseller",
            "pg_permissions": ["pg_admins", "pg_nodes"],  # elevated cookie
            "pg_client_ready": True,
        }
        with patch(
            "app.services.authz.load_live_pg_acl_for_role_id",
            new=AsyncMock(return_value=(["pg_users"], _role(users=True))),
        ):
            out = await enrich_reseller_actor(AsyncMock(), stale, profile)
        self.assertEqual(out["pg_permissions"], ["pg_users"])
        self.assertNotIn("pg_admins", out["pg_permissions"])
        self.assertTrue(out["pg_client_ready"])

    async def test_enrich_without_password_not_ready(self):
        from app.services.authz import enrich_pg_staff_actor

        row = SimpleNamespace(
            id=1,
            pg_username="lim",
            pg_password_enc=None,
            web_username="lim",
        )
        with (
            patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=9),
            ),
            patch(
                "app.services.authz.load_live_pg_acl_for_role_id",
                new=AsyncMock(return_value=(["pg_users", "pg_overview"], _role(users=True, system=True))),
            ),
        ):
            out = await enrich_pg_staff_actor(AsyncMock(), {"role": "pg_staff"}, row)
        self.assertFalse(out["pg_client_ready"])
        self.assertIn("pg_users", out["pg_permissions"])


class StaticArchitectureGuards(unittest.TestCase):
    def test_no_session_none_owner_branch_in_assert_owned(self):
        src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        start = src.index("async def _assert_owned_user")
        body = src[start : start + 1200]
        self.assertIn("assert_staff_pg_session", body)
        self.assertNotIn("or session is None", body)

    def test_staff_pg_uses_resolve_pg_client(self):
        src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn("resolve_pg_client", src)


if __name__ == "__main__":
    unittest.main()
