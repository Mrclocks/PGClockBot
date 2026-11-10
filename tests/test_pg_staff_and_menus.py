"""Tests for PasarGuard role→feature mapping and staff web access helpers."""

from __future__ import annotations

import unittest
from pathlib import Path


class MapPgRoleTests(unittest.TestCase):
    def test_owner_gets_all_features_except_admins(self):
        from app.services.pg_access import PG_FEATURE_KEYS, map_pg_role_to_features

        feats = map_pg_role_to_features({"is_owner": True, "permissions": {}})
        self.assertEqual(feats, list(PG_FEATURE_KEYS))
        self.assertNotIn("pg_admins", feats)

    def test_limited_role_maps_resources(self):
        from app.services.pg_access import map_pg_role_to_features, role_user_actions

        role = {
            "permissions": {
                "system": {"read": True},
                "users": {"read": True, "create": True, "update": False, "delete": False},
                "nodes": {"read": {"scope": 1}},
            }
        }
        feats = map_pg_role_to_features(role)
        self.assertIn("pg_overview", feats)
        self.assertIn("pg_users", feats)
        self.assertIn("pg_nodes", feats)
        self.assertNotIn("pg_templates", feats)
        actions = role_user_actions(role)
        self.assertTrue(actions["create"])
        self.assertTrue(actions["read"])
        self.assertFalse(actions["delete"])

    def test_empty_role(self):
        from app.services.pg_access import map_pg_role_to_features

        self.assertEqual(map_pg_role_to_features(None), [])
        self.assertEqual(map_pg_role_to_features({}), [])


class SidebarMenuOrderTests(unittest.TestCase):
    def test_bot_sidebar_order_and_labels(self):
        from tests.sidebar_view import labels, owner_sidebar

        nav = owner_sidebar()
        # Orders + payments are a single «مالی» entry pointing at /finance.
        self.assertEqual(
            labels(nav["shop"]),
            ["نمای کلی فروشگاه", "کاربران ربات", "نمایندگان", "مالی", "پلن‌ها", "پشتیبانی", "بازاریابی",
             "گزارش کارکنان", "تنظیمات ربات"],
        )
        self.assertEqual(nav["shop"][0][1], "/dashboard")
        self.assertNotIn("تیکت‌ها", labels(nav["shop"]))
        self.assertEqual(labels(nav["system"]), ["داشبورد", "مرکز اقدام", "تنظیمات سیستم"])

    def test_pg_sidebar_order_and_labels(self):
        from tests.sidebar_view import labels, owner_sidebar

        self.assertEqual(
            labels(owner_sidebar()["pg"])[:8],
            ["نمای کلی پاسارگارد", "کاربران", "ادمین‌ها", "نودها", "گروه‌ها", "هاست‌ها", "تمپلیت‌ها", "اینباندها"],
        )

    def test_pg_tabs_macro_order(self):
        src = Path("app/web/templates/macros.html").read_text(encoding="utf-8")
        start = src.find("macro pg_tabs")
        end = src.find("endmacro", start)
        chunk = src[start:end] if end > start else src[start:]
        labels = ["نمای کلی", "کاربران", "ادمین", "نود", "گروه", "هاست", "تمپلیت", "اینباند"]
        positions = [chunk.find(f">{l}</a>") for l in labels]
        self.assertTrue(all(p >= 0 for p in positions), positions)
        self.assertEqual(positions, sorted(positions))


class PgStaffAccessServiceTests(unittest.TestCase):
    def test_module_exports(self):
        from app.services import pg_staff_access as m

        for name in (
            "upsert_web_access",
            "revoke_web_access",
            "access_by_web_username",
            "resolve_pg_role_id_for_admin",
            "change_staff_credentials",
        ):
            self.assertTrue(hasattr(m, name), name)

    def test_model_exists(self):
        from app.db.models import PgStaffAccess

        self.assertEqual(PgStaffAccess.__tablename__, "pg_staff_access")

    def test_conflict_helpers_exported(self):
        from app.services import pg_staff_access as m

        for name in (
            "grant_web_access",
            "conflict_message_for_new_grant",
            "conflict_message_for_reseller_link",
            "web_access_status_map",
            "resolve_existing_web_access",
        ):
            self.assertTrue(hasattr(m, name), name)


if __name__ == "__main__":
    unittest.main()
