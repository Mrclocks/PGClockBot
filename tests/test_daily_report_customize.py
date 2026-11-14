"""Daily report customization — role-scoped metrics + safe templates."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DailyReportCatalogTests(unittest.TestCase):
    def test_tab_is_the_last_bot_settings_tab(self):
        from app.services.resellers import RESELLER_SETTINGS_TABS
        from app.services.users import SETTINGS_TABS

        # The Owner's bot connection moved to system settings; a reseller keeps their own bot tab last.
        self.assertEqual([k for k, _ in SETTINGS_TABS][-1], "daily_report")
        keys = [k for k, _ in RESELLER_SETTINGS_TABS]
        self.assertEqual(keys[-1], "bot")
        self.assertEqual(keys[-2], "daily_report")

    def test_notifications_no_longer_owns_daily_keys(self):
        from app.services.users import SETTING_GROUPS, TAB_SETTING_GROUPS, keys_for_tab

        self.assertIn("گزارش روزانه", SETTING_GROUPS)
        self.assertIn("daily_report", TAB_SETTING_GROUPS)
        notif_keys = keys_for_tab("notifications")
        self.assertNotIn("admin_daily_report_enabled", notif_keys)
        daily_keys = keys_for_tab("daily_report")
        self.assertIn("admin_daily_report_enabled", daily_keys)
        self.assertIn("admin_daily_report_template", daily_keys)

    def test_shop_cannot_enable_owner_only_metric(self):
        from app.services.daily_report import (
            ACTOR_OWNER,
            ACTOR_SHOP,
            parse_metric_keys,
        )

        shop = parse_metric_keys("resellers_active,users_new", actor=ACTOR_SHOP)
        self.assertNotIn("resellers_active", shop)
        self.assertIn("users_new", shop)
        owner = parse_metric_keys("resellers_active,users_new", actor=ACTOR_OWNER)
        self.assertIn("resellers_active", owner)

    def test_template_blanks_disabled_keys(self):
        from app.services.daily_report import render_daily_report_template

        out = render_daily_report_template(
            "A:{users_new} B:{orders_new}",
            {"users_new": "3", "orders_new": "9"},
            enabled_keys=["users_new"],
        )
        self.assertIn("A:3", out)
        self.assertIn("B:", out)
        self.assertNotIn("9", out)

    def test_jalali_smoke(self):
        from app.services.daily_report import gregorian_to_jalali

        jy, jm, jd = gregorian_to_jalali(2026, 8, 21)
        self.assertEqual((jy, jm, jd), (1405, 5, 30))

    def test_templates_and_scheduler_wired(self):
        self.assertTrue(
            (ROOT / "app/web/templates/_settings_daily_report.html").exists()
        )
        settings = (ROOT / "app/web/templates/settings.html").read_text(encoding="utf-8")
        self.assertIn("daily_report", settings)
        shop = (ROOT / "app/web/templates/shop_settings.html").read_text(encoding="utf-8")
        self.assertIn("daily_report", shop)
        sched = (ROOT / "app/jobs/scheduler.py").read_text(encoding="utf-8")
        self.assertIn("shop_daily_report_chat_ids", sched)
        self.assertIn("ACTOR_SHOP", sched)
        self.assertIn("open_notify_bot_for_reseller", sched)

    def test_preview_js_has_daily_report(self):
        js = (ROOT / "app/web/templates/_tg_preview_chat_js.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("daily_report", js)
        self.assertIn("fillDailyReport", js)

    def test_message_variables_catalog_includes_daily_report(self):
        from app.services.message_variables import (
            DOMAIN_DAILY_REPORT,
            SETTING_DOMAIN,
            catalog_groups,
        )

        self.assertEqual(
            SETTING_DOMAIN.get("admin_daily_report_template"), DOMAIN_DAILY_REPORT
        )
        labels = {g["domain"] for g in catalog_groups(include_owner_only=True)}
        self.assertIn(DOMAIN_DAILY_REPORT, labels)
        shop = catalog_groups(include_owner_only=False)
        shop_keys = {
            v["key"]
            for g in shop
            if g["domain"] == DOMAIN_DAILY_REPORT
            for v in g["vars"]
        }
        self.assertIn("users_new", shop_keys)
        self.assertNotIn("resellers_active", shop_keys)


if __name__ == "__main__":
    unittest.main()
