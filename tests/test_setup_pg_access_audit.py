"""Setup wizard PG access audit + Hybrid Owner representative gate."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.bot.keyboards import admin_reply_keyboard, main_reply_keyboard
from app.services.identity_chrome import hierarchy_identity
from app.services.pg_access import (
    public_pg_access_audit,
    staff_from_platform_caps,
    staff_has_pg_admins_create,
)
from app.services.principal_provisioning import owner_has_pg_admin_create_capability
from app.services.representative_unification import (
    assert_staff_can_manage_representatives,
    staff_can_manage_representatives,
    RepresentativeUnifyError,
)


ROOT = Path(__file__).resolve().parents[1]


def _limited_caps() -> dict:
    return {
        "ok": True,
        "error": None,
        "username": "shopadmin",
        "pg_is_owner": False,
        "pg_role_id": 9,
        "features": ["pg_overview", "pg_users"],
        "role": {
            "id": 9,
            "name": "Seller",
            "is_owner": False,
            "limits": {"max_users": 30, "data_limit_max": 3 * (1024**3)},
            "permissions": {
                "users": {"read": True, "create": True},
                "admins": {"create": False},
            },
        },
        "admin": {
            "username": "shopadmin",
            "total_users": 4,
            "data_limit": 30 * (1024**3),
            "used_traffic": 5 * (1024**3),
        },
    }


class PublicPgAccessAuditTests(unittest.TestCase):
    def test_limited_report_has_no_secrets(self) -> None:
        audit = public_pg_access_audit(_limited_caps())
        self.assertTrue(audit["ok"])
        self.assertFalse(audit["pg_is_owner"])
        self.assertEqual(audit["username"], "shopadmin")
        self.assertEqual(audit["role_name"], "Seller")
        self.assertFalse(audit["can_create_admin"])
        self.assertFalse(audit["can_manage_representatives"])
        self.assertTrue(audit["shop_full"])
        labels = {f["label"] for f in audit["features"]}
        self.assertIn("کاربران", labels)
        self.assertNotIn("ادمین", labels)
        blob = str(audit)
        self.assertNotIn("password", blob.lower())
        limit_labels = {row["label"] for row in audit["limits"]}
        self.assertIn("سقف کاربران", limit_labels)
        self.assertIn("سقف حجم حساب", limit_labels)

    def test_owner_report_is_unrestricted(self) -> None:
        audit = public_pg_access_audit(
            {
                "ok": True,
                "username": "root",
                "pg_is_owner": True,
                "features": [],
                "role": {"is_owner": True, "name": "Owner"},
                "admin": {"username": "root"},
            }
        )
        self.assertTrue(audit["pg_is_owner"])
        self.assertTrue(audit["can_manage_representatives"])
        keys = {f["key"] for f in audit["features"]}
        self.assertIn("pg_admins", keys)
        self.assertIn("pg_users", keys)

    def test_failed_probe_fail_closed(self) -> None:
        audit = public_pg_access_audit(
            {"ok": False, "error": "bad login", "username": "x"}
        )
        self.assertFalse(audit["ok"])
        self.assertFalse(audit["can_manage_representatives"])
        self.assertEqual(audit["features"], [])

    def test_admins_create_on_limited_role(self) -> None:
        caps = _limited_caps()
        caps["role"]["permissions"]["admins"] = {"create": True}
        audit = public_pg_access_audit(caps)
        self.assertTrue(audit["can_create_admin"])
        self.assertTrue(audit["can_manage_representatives"])
        staff = staff_from_platform_caps(caps)
        self.assertTrue(staff_has_pg_admins_create(staff))


class HybridOwnerRepGateTests(unittest.TestCase):
    def _owner(self, **extra) -> dict:
        base = {
            "role": "admin",
            "web_owner": True,
            "org_depth": 0,
            "org_parent_id": None,
            "org_principal_id": 1,
            "org_status": "active",
        }
        base.update(extra)
        return base

    def test_true_owner_can_manage(self) -> None:
        staff = self._owner(pg_is_owner=True)
        self.assertTrue(owner_has_pg_admin_create_capability(staff))
        self.assertTrue(staff_can_manage_representatives(staff))
        assert_staff_can_manage_representatives(staff)

    def test_unenriched_owner_keeps_sudo_default(self) -> None:
        staff = self._owner()
        self.assertTrue(owner_has_pg_admin_create_capability(staff))
        self.assertTrue(staff_can_manage_representatives(staff))

    def test_hybrid_without_create_denied(self) -> None:
        staff = self._owner(
            pg_is_owner=False,
            pg_actions={"admins": {"create": False}},
            pg_role={"permissions": {"users": {"create": True}}},
        )
        self.assertFalse(owner_has_pg_admin_create_capability(staff))
        self.assertFalse(staff_can_manage_representatives(staff))
        ident = hierarchy_identity(staff)
        self.assertFalse(ident["can_manage_representatives"])
        with self.assertRaises(RepresentativeUnifyError) as ctx:
            assert_staff_can_manage_representatives(staff)
        self.assertEqual(ctx.exception.code, "pg_capability_denied")

    def test_hybrid_with_create_allowed(self) -> None:
        staff = self._owner(
            pg_is_owner=False,
            pg_actions={"admins": {"create": True}},
        )
        self.assertTrue(staff_can_manage_representatives(staff))
        ident = hierarchy_identity(staff)
        self.assertTrue(ident["can_manage_representatives"])
        self.assertFalse(ident["can_add_representative"])


class WizardAndKeyboardSurfaceTests(unittest.TestCase):
    def test_setup_template_has_probe_and_audit(self) -> None:
        html = (ROOT / "app/web/templates/setup.html").read_text(encoding="utf-8")
        self.assertIn("بررسی سطح دسترسی", html)
        self.assertIn("setup-probe", html)
        self.assertIn("X-Requested-With", html)
        self.assertIn("setup-probe", html)
        self.assertIn("pg_audit", html)
        self.assertIn("can_manage_representatives", html)
        self.assertNotIn("flash_warn", html)
        self.assertIn('name="pg_password" type="password" value=""', html)

    def test_setup_other_single_endpoint(self) -> None:
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn('x-requested-with', src.lower())
        self.assertIn("setup-probe", src)
        self.assertNotIn("/setup/pg-probe", src)
        self.assertNotIn("pg_warn", src)
        self.assertIn("setup_pg_access_audit", src)

    def test_admin_keyboard_hides_resellers_when_denied(self) -> None:
        from app.bot.keyboards import admin_people_reply_keyboard

        ui = {"btn_adm_orders": "سفارش", "btn_adm_payments": "پرداخت", "btn_adm_tickets": "تیکت",
              "btn_adm_plans": "پلن", "btn_adm_users": "کاربران", "btn_adm_pg": "پاسارگارد",
              "btn_adm_settings": "تنظیمات", "btn_adm_broadcast": "پیام", "btn_adm_preview": "پیش‌نمایش",
              "btn_back": "بازگشت", "btn_menu_home": "خانه"}
        # Hub always shows «افراد»; ACL hides نمایندگان inside the people group
        hub_hidden = [b.text for row in admin_reply_keyboard(ui, can_manage_representatives=False).keyboard for b in row]
        hub_shown = [b.text for row in admin_reply_keyboard(ui, can_manage_representatives=True).keyboard for b in row]
        self.assertIn("👤 افراد", hub_hidden)
        self.assertIn("👤 افراد", hub_shown)
        hidden = [b.text for row in admin_people_reply_keyboard(ui, can_manage_representatives=False).keyboard for b in row]
        shown = [b.text for row in admin_people_reply_keyboard(ui, can_manage_representatives=True).keyboard for b in row]
        self.assertNotIn("🤝 نمایندگان", hidden)
        self.assertIn("🤝 نمایندگان", shown)
        main_hidden = [b.text for row in main_reply_keyboard("admin", ui=ui, can_manage_representatives=False).keyboard for b in row]
        self.assertNotIn("🤝 نمایندگان", main_hidden)
        # The admin main menu only carries the entry to the admin hub.
        self.assertIn("🛠 پنل ادمین", main_hidden)

    def test_sidebar_owner_resellers_uses_capability(self) -> None:
        base = (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8")
        self.assertIn("ident.can_manage_representatives", base)
        self.assertIn('require_pg_perm("pg_admins")', (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8"))
        resellers = (ROOT / "app/api/reseller_pages.py").read_text(encoding="utf-8")
        self.assertIn("_require_rep_mgmt", resellers)
        self.assertIn("assert_staff_can_manage_representatives", resellers)

    def test_bot_home_uses_live_admin_keyboard(self) -> None:
        start = (ROOT / "app/bot/handlers/start.py").read_text(encoding="utf-8")
        self.assertIn("build_main_reply_keyboard", start)
        nav = (ROOT / "app/bot/menu_nav.py").read_text(encoding="utf-8")
        self.assertIn("async def admin_hub_reply_keyboard", nav)
        admin = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        self.assertNotIn("kb.admin_reply_keyboard()", admin)

    def test_plans_use_live_quota_validation(self) -> None:
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("assert_user_plan_within_limits", src)
        self.assertIn("assert_custom_plan_range_within_limits", src)
        plans = (ROOT / "app/web/templates/plans.html").read_text(encoding="utf-8")
        self.assertIn("plan_limit_issues", plans)
        self.assertIn("pg_limit_snapshot", plans)
        reseller = (ROOT / "app/api/reseller_pages.py").read_text(encoding="utf-8")
        self.assertIn("_validate_reseller_capacity_inputs", reseller)


class ResolveProbeUsesOwnClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_probe_passes_temporary_client(self) -> None:
        from app.services.pg_access import (
            clear_platform_pg_capability_cache,
            resolve_platform_pg_capabilities,
        )

        clear_platform_pg_capability_cache()
        captured: dict = {}

        async def fake_resolve(role_id, *, client=None):
            captured["role_id"] = role_id
            captured["client"] = client
            return ["pg_users"], {
                "id": 9,
                "is_owner": False,
                "permissions": {"users": {"read": True, "create": True}},
            }

        fake_client = AsyncMock()
        fake_client.ensure_token = AsyncMock(return_value="tok")
        fake_client.get_admin = AsyncMock(
            return_value={"username": "lim", "role_id": 9, "is_sudo": False}
        )
        fake_client.close = AsyncMock()
        fake_client.base_url = "https://pg.example"
        fake_client._client = AsyncMock()
        fake_client._client.aclose = AsyncMock()

        with (
            patch("app.services.pasarguard.PasarGuardClient", return_value=fake_client),
            patch(
                "app.services.pg_access.resolve_reseller_pg_features",
                new=AsyncMock(side_effect=fake_resolve),
            ),
            patch("app.config.get_settings") as gs,
        ):
            gs.return_value.pg_username = "env"
            gs.return_value.pg_password = "envpw"
            gs.return_value.pg_base_url = "https://stale.example"
            caps = await resolve_platform_pg_capabilities(
                username="lim",
                password="secret",
                base_url="https://pg.example",
                use_cache=False,
            )
        self.assertTrue(caps["ok"])
        self.assertIs(captured["client"], fake_client)
        self.assertEqual(captured["role_id"], 9)


class LimitedAdminSelfLoginTests(unittest.IsolatedAsyncioTestCase):
    def _own_client(self, **methods) -> AsyncMock:
        fake_client = AsyncMock()
        fake_client.ensure_token = AsyncMock(return_value="tok")
        fake_client.close = AsyncMock()
        fake_client.base_url = "https://pg.example"
        fake_client._client = AsyncMock()
        fake_client._client.aclose = AsyncMock()
        for name, value in methods.items():
            setattr(fake_client, name, value)
        return fake_client

    async def test_directory_miss_uses_current_admin_nested_role(self) -> None:
        """Limited PG admin (users.create only): token OK, /api/admins 403.

        Setup must succeed via GET /api/admin and stay Hybrid (not PG owner).
        """
        from app.services.pg_access import (
            clear_platform_pg_capability_cache,
            resolve_platform_pg_capabilities,
            staff_from_platform_caps,
            staff_has_pg_admins_create,
        )

        clear_platform_pg_capability_cache()
        nested = {
            "id": 9,
            "name": "Seller",
            "is_owner": False,
            "permissions": {"users": {"create": True}},
        }
        fake_client = self._own_client(
            get_current_admin=AsyncMock(
                return_value={"username": "84104", "role": nested, "is_sudo": False}
            ),
            get_admin=AsyncMock(return_value=None),
        )
        with (
            patch("app.services.pasarguard.PasarGuardClient", return_value=fake_client),
            patch(
                "app.services.pg_access.resolve_reseller_pg_features",
                new=AsyncMock(return_value=([], None)),
            ),
            patch("app.config.get_settings") as gs,
        ):
            gs.return_value.pg_username = "env"
            gs.return_value.pg_password = "envpw"
            gs.return_value.pg_base_url = "https://stale.example"
            caps = await resolve_platform_pg_capabilities(
                username="84104",
                password="secret",
                base_url="https://pg.example",
                use_cache=False,
            )
        self.assertTrue(caps["ok"])
        self.assertIsNone(caps.get("error"))
        self.assertEqual(caps["username"], "84104")
        self.assertFalse(caps["pg_is_owner"])
        self.assertIn("pg_users", caps["features"])
        self.assertNotIn("pg_admins", caps["features"])
        staff = staff_from_platform_caps(caps)
        self.assertFalse(staff_has_pg_admins_create(staff))
        fake_client.get_admin.assert_not_awaited()

    async def test_token_ok_stubs_admin_when_self_and_directory_fail(self) -> None:
        from app.services.pg_access import (
            clear_platform_pg_capability_cache,
            resolve_platform_pg_capabilities,
        )

        clear_platform_pg_capability_cache()
        fake_client = self._own_client(
            get_current_admin=AsyncMock(return_value=None),
            get_admin=AsyncMock(return_value=None),
        )
        with (
            patch("app.services.pasarguard.PasarGuardClient", return_value=fake_client),
            patch(
                "app.services.pg_access.resolve_reseller_pg_features",
                new=AsyncMock(return_value=([], None)),
            ),
            patch("app.config.get_settings") as gs,
        ):
            gs.return_value.pg_username = "env"
            gs.return_value.pg_password = "envpw"
            gs.return_value.pg_base_url = "https://stale.example"
            caps = await resolve_platform_pg_capabilities(
                username="lim",
                password="secret",
                base_url="https://pg.example",
                use_cache=False,
            )
        self.assertTrue(caps["ok"])
        self.assertEqual(caps["username"], "lim")
        self.assertEqual(caps["features"], [])
        self.assertFalse(caps["pg_is_owner"])
        self.assertEqual((caps.get("admin") or {}).get("username"), "lim")


if __name__ == "__main__":
    unittest.main()
