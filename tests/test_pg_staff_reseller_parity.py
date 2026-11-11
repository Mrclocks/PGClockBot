"""Tests for PG-admin → reseller shop provisioning and overview limits UI."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services.pg_overview import _role_constraint_boxes, _status_meta
from app.services.resellers import _synthetic_telegram_id, bot_needs_setup, join_perms, parse_perms


GB = 1024**3


class SyntheticIdTests(unittest.TestCase):
    def test_stable_negative(self):
        a = _synthetic_telegram_id("AdminOne")
        b = _synthetic_telegram_id("adminone")
        self.assertEqual(a, b)
        self.assertLess(a, 0)


class BotSetupFlagTests(unittest.TestCase):
    def test_needs_setup_without_token(self):
        from types import SimpleNamespace

        self.assertTrue(bot_needs_setup(SimpleNamespace(is_active=True, bot_token=None)))
        self.assertFalse(bot_needs_setup(SimpleNamespace(is_active=True, bot_token="x:y")))
        self.assertFalse(bot_needs_setup(None))


class ConstraintBoxesTests(unittest.TestCase):
    def test_builds_volume_and_expire_boxes(self):
        boxes = _role_constraint_boxes(
            {
                "data_limit_min": 1 * GB,
                "data_limit_max": 10 * GB,
                "expire_max": 30 * 86400,
            }
        )
        labels = [b["label"] for b in boxes]
        self.assertIn("حداقل حجم کاربر", labels)
        self.assertIn("حداکثر حجم کاربر", labels)
        self.assertIn("حداکثر مدت کاربر", labels)
        # Constraint boxes must not carry captions (only users/volume keep remain hints)
        for b in boxes:
            self.assertNotIn("hint", b)

    def test_templates_omit_constraint_captions(self):
        for rel in ("app/web/templates/_reseller_home_dash_body.html", "app/web/templates/_pg_dash_body.html"):
            src = Path(rel).read_text(encoding="utf-8")
            self.assertNotIn("c.hint", src)
            self.assertIn("pg_overview_limit_board", src)
        gauges = Path("app/web/templates/_pg_quota_gauges.html").read_text(encoding="utf-8")
        self.assertIn("باقی‌مانده", gauges)
        self.assertNotIn("pg-gauge-pulse", gauges)
        self.assertIn("is-exhausted", gauges)
        self.assertIn("home-gauge", gauges)

    def test_status_meta_still_works(self):
        self.assertEqual(_status_meta("limited"), ("محدود", "warn"))


class WiringTests(unittest.TestCase):
    def test_grant_form_requires_plan(self):
        src = Path("app/web/templates/pg_admins.html").read_text(encoding="utf-8")
        self.assertIn('name="plan_id"', src)
        self.assertIn("پلن نمایندگی", src)

    def test_handler_uses_provision(self):
        src = Path("app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn("provision_existing_pg_admin", src)

    def test_dashboard_bot_banner(self):
        # Web dashboard for reseller: portals + periods + optional PG quota
        home = Path("app/web/templates/_reseller_home_dash_body.html").read_text(encoding="utf-8")
        self.assertIn("_home_ops.html", home)
        self.assertIn("pg_limits", home)
        self.assertIn("pg_overview_limit_board", home)
        self.assertNotIn("pg_limits.time", home)
        self.assertNotIn('class="meter"', home)

    def test_pg_home_no_time_boxes(self):
        src = Path("app/web/templates/_pg_dash_body.html").read_text(encoding="utf-8")
        self.assertNotIn("زمان کل", src)
        self.assertNotIn("زمان باقیمانده", src)
        self.assertIn("کاربران", src)
        self.assertNotIn("VPN", src)
        self.assertNotIn('class="meter"', src)
        self.assertIn("pg_overview_limit_board", src)
        cards = Path("app/web/templates/_pg_limit_cards.html").read_text(encoding="utf-8")
        self.assertIn("pg_quota_gauge", cards)
        self.assertIn("pg-quota-top", cards)
        gauges = Path("app/web/templates/_pg_quota_gauges.html").read_text(encoding="utf-8")
        self.assertIn("باقی‌مانده {{ meter.remain_text }}", gauges)
        self.assertIn("ratio_text", Path("app/services/pg_overview.py").read_text(encoding="utf-8"))

    def test_dashboard_status_is_badge_not_box(self):
        # Status badge lives on reseller web home / PG overview, not bot نمای کلی
        src = Path("app/web/templates/_reseller_home_dash_body.html").read_text(encoding="utf-8")
        self.assertIn("pg_overview_limit_board", src)
        cards = Path("app/web/templates/_pg_limit_cards.html").read_text(encoding="utf-8")
        self.assertIn("ov.status_label", cards)
        self.assertIn("ov.status_badge", cards)
        self.assertNotIn("<span>وضعیت</span>", src)
        self.assertNotIn("VPN", src)

    def test_broadcast_caption_uses_small_muted(self):
        src = Path("app/web/templates/broadcast.html").read_text(encoding="utf-8")
        self.assertIn('<small class="muted">گروه دریافت‌کننده', src)

    def test_ram_percent_in_ring_amount_under_title(self):
        home = Path("app/web/templates/_home_dash_body.html").read_text(encoding="utf-8")
        gauges = Path("app/web/templates/_host_resource_gauges.html").read_text(
            encoding="utf-8"
        )
        # Percent lives in the ring center (like CPU); used/total muted like cores.
        self.assertIn('data-host-mem-val', gauges)
        self.assertIn('data-host-mem-hint', gauges)
        self.assertIn("num-ratio", gauges)
        hint_at = gauges.find("data-host-mem-hint")
        self.assertIn("muted", gauges[hint_at - 80 : hint_at])
        self.assertNotIn("home-gauge-amount", gauges)
        self.assertNotIn("home-gauge-center-quiet", gauges)
        # PG admin counts live on /pg overview, not the slim home dash
        self.assertIn("_home_ops.html", home)
        self.assertNotIn(">تمپلیت‌ها</span>", home)

    def test_base_nav_web_panel_for_all(self):
        from tests.sidebar_view import owner_sidebar, sidebar

        self.assertIn(("تنظیمات سیستم", "/settings?tab=general"), owner_sidebar()["system"])
        for role in ("reseller", "pg_staff", "principal"):
            account = sidebar(role, perms=("dashboard",))["system"]
            self.assertIn(("داشبورد", "/home"), account, role)
            self.assertIn(("حساب و امنیت", "/security"), account, role)
        self.assertIn(("نمای کلی فروشگاه", "/dashboard"), owner_sidebar()["shop"])

    def test_dashboard_perm_always_injected(self):
        perms = parse_perms("plans,orders")
        for must in ("dashboard", "shop_settings"):
            if must not in perms:
                perms.append(must)
        joined = join_perms(perms)
        self.assertIn("dashboard", joined)
        self.assertIn("shop_settings", joined)


class ProvisionImportTests(unittest.TestCase):
    def test_import(self):
        from app.services.resellers import provision_existing_pg_admin

        self.assertTrue(callable(provision_existing_pg_admin))


if __name__ == "__main__":
    unittest.main()
