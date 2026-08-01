"""Dashboard overview polish — گیگ label, sticky footer, equal boxes."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.services.formatting import format_bytes, format_bytes_ratio, format_count_ratio, format_gb


GB = 1024**3
MB = 1024**2


class FormatGigLabelTests(unittest.TestCase):
    def test_gb_is_persian_gig(self):
        text = format_bytes(5 * GB)
        self.assertIn("گیگ", text)
        self.assertNotIn("GB", text)

    def test_format_gb_uses_gig(self):
        self.assertIn("گیگ", format_gb(10))
        self.assertNotIn("GB", format_gb(10))

    def test_byte_and_meg_labels(self):
        self.assertIn("بایت", format_bytes(500))
        self.assertIn("مگ", format_bytes(50 * MB))

    def test_unlimited(self):
        self.assertEqual(format_bytes(None), "نامحدود")

    def test_bytes_ratio_single_unit(self):
        text = format_bytes_ratio(10 * GB, 100 * GB)
        self.assertEqual(text, "10/100 گیگ")
        self.assertNotIn("گیگ /", text)
        self.assertEqual(text.count("گیگ"), 1)

    def test_bytes_ratio_meg(self):
        text = format_bytes_ratio(256 * MB, 512 * MB)
        self.assertIn("مگ", text)
        self.assertRegex(text, r"256/512 مگ")

    def test_count_ratio(self):
        self.assertEqual(format_count_ratio(10, 100), "10/100")


class DashboardPolishSourceTests(unittest.TestCase):
    def test_bot_setup_centered(self):
        css = Path("app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(".dash-bot-setup-inner", css)
        self.assertIn("justify-content: center", css)
        self.assertIn(".site-footer {\n  margin-top: auto;", css)

    def test_overview_boxes_equal_and_rtl(self):
        css = Path("app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn("height: 96px", css)
        self.assertIn("justify-content: space-between", css)
        self.assertIn("text-overflow: ellipsis", css)
        self.assertIn("text-align: right", css)

    def test_home_and_dashboard_share_panel_classes(self):
        home = Path("app/web/templates/home.html").read_text(encoding="utf-8")
        dash = Path("app/web/templates/dashboard.html").read_text(encoding="utf-8")
        reseller_home = Path("app/web/templates/reseller_home.html").read_text(encoding="utf-8")
        for cls in ("home-panels", "home-panel-bot", "home-panel-grid"):
            self.assertIn(cls, home)
            self.assertIn(cls, dash)
            self.assertIn(cls, reseller_home)
        self.assertIn("home-panel-pg", home)
        self.assertIn("home-panel-pg", reseller_home)
        self.assertNotIn("home-panel-pg", dash)

    def test_templates_use_ratio_text(self):
        reseller_home = Path("app/web/templates/reseller_home.html").read_text(encoding="utf-8")
        pg = Path("app/web/templates/pg_home.html").read_text(encoding="utf-8")
        self.assertIn("ratio_text", reseller_home)
        self.assertIn("ratio_text", pg)
        self.assertNotIn("used_text }} / {{", reseller_home)
        self.assertNotIn("used_text }} / {{", pg)

    def test_web_panel_has_no_vpn_label(self):
        for path in Path("app/web/templates").rglob("*.html"):
            src = path.read_text(encoding="utf-8")
            self.assertNotIn("VPN", src, msg=f"VPN still in {path}")


if __name__ == "__main__":
    unittest.main()
