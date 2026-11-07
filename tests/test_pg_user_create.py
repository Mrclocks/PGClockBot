"""PG user create (template + custom) wiring tests."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock


class PgUserCreateUiTests(unittest.TestCase):
    def test_template_has_custom_mode(self):
        src = Path("app/web/templates/pg_users.html").read_text(encoding="utf-8")
        self.assertIn('name="mode"', src)
        self.assertIn("data_limit_gb", src)
        self.assertIn("duration_days", src)
        self.assertIn("pg-user-mode-custom", src)
        self.assertIn("گروه + حجم + مدت", src)
        self.assertIn("modal-pg-user-link", src)
        self.assertIn("modal-pg-user-edit", src)
        self.assertIn("ویرایش", src)
        self.assertIn("/pg/users/", src)

    def test_post_handler_supports_custom(self):
        src = Path("app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn("create_user_from_template", src)
        self.assertIn("create_user(", src)
        self.assertIn("groups_allowed_for_staff", src)
        self.assertIn("require_template", src)
        self.assertIn("parse_group_ids_from_form", src)
        self.assertIn("modify_user_by_id", src)
        self.assertIn("/pg/users/{user_id}/edit", src)
        self.assertIn("/pg/users/{user_id}/link", src)
        self.assertIn("build_user_create_payload", src)
        self.assertIn("days * 86400", src)

    def test_create_payload_has_proxy_settings(self):
        from app.services.pasarguard import build_user_create_payload

        p = build_user_create_payload(username="user_01", group_ids=[1], expire_ts=1_700_000_000)
        self.assertEqual(p["proxy_settings"], {})
        self.assertEqual(p["group_ids"], [1])
        self.assertIn("T", str(p["expire"]))  # ISO datetime

    def test_nodes_reconnect_for_viewers(self):
        src = Path("app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn("can_reconnect", src)
        self.assertIn("reconnect_node", src)
        tpl = Path("app/web/templates/pg_nodes.html").read_text(encoding="utf-8")
        self.assertIn("اتصال مجدد", tpl)
        self.assertIn("can_reconnect", tpl)

    def test_panel_settings_tabs(self):
        from tests.sidebar_view import owner_sidebar

        self.assertEqual(set(owner_sidebar()), {"system", "shop", "pg"})
        tabs = Path("app/web/templates/_panel_settings_tabs.html").read_text(encoding="utf-8")
        self.assertIn("tab=backup", tabs)
        self.assertIn("/security", tabs)
        self.assertIn("آپدیت", tabs)


class PgUserCreateLogicTests(unittest.TestCase):
    def test_groups_allowed_helpers(self):
        from app.services.plans_catalog import groups_allowed_for_staff, template_allowed_for_staff

        admin = {"role": "admin"}
        self.assertTrue(groups_allowed_for_staff(admin, [1, 2]))
        self.assertTrue(template_allowed_for_staff(admin, 9))

        reseller = {
            "role": "reseller",
            "pg_access": {"allowed_group_ids": [3, 5], "allowed_template_ids": [10]},
        }
        self.assertTrue(groups_allowed_for_staff(reseller, [3]))
        self.assertFalse(groups_allowed_for_staff(reseller, [3, 9]))
        self.assertTrue(template_allowed_for_staff(reseller, 10))
        self.assertFalse(template_allowed_for_staff(reseller, 11))


if __name__ == "__main__":
    unittest.main()
