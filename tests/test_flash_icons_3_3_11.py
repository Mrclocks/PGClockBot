"""3.3.11 — clean exhausted border + unified flash severity icons."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.services.pg_overview import _meter


ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")


class ExhaustedBorderTests(unittest.TestCase):
    def test_no_shadow_or_gradient_on_exhausted(self):
        block = CSS.split(".home-gauge.is-exhausted {")[1].split("}")[0]
        self.assertIn("border-color:", block)
        self.assertNotIn("box-shadow", block)
        self.assertNotIn("linear-gradient", block)
        self.assertNotIn("background:", block)

    def test_meter_exhausted_flag(self):
        m = _meter(
            label="حجم",
            used=10,
            limit=10,
            used_label="مصرف‌شده",
            remain_label="باقی‌مانده",
            format_value=str,
            kind="bytes",
        )
        self.assertTrue(m["exhausted"])


class FlashIconTests(unittest.TestCase):
    def test_severity_icons_defined(self):
        self.assertIn(".flash.ok::before", CSS)
        self.assertIn(".flash.warn::before", CSS)
        self.assertIn(".flash.err::before", CSS)
        # Shapes encoded in SVG data URIs
        self.assertIn("M8 12.2l2.8 2.8L16.5 9", CSS)  # square check (ok)
        self.assertIn("M12 3.8L21 19.2H3L12 3.8z", CSS)  # triangle (warn)
        self.assertIn("circle cx='12' cy='12' r='8.5'", CSS)  # circle (err)
        self.assertIn("M9 9l6 6M15 9l-6 6", CSS)
        # Icon box size matches stat-ico
        shared = CSS.split(".flash.ok::before,")[1].split("/* Green:")[0]
        self.assertIn("width: 28px", shared)
        self.assertIn("height: 28px", shared)
        self.assertIn("border-radius: var(--radius-pill)", shared)

    def test_no_legacy_pulse_or_quota_banner_ico(self):
        self.assertNotIn("pg-gauge-pulse", CSS)
        self.assertNotIn("pg-quota-banner-ico", CSS)
        macro = (ROOT / "app/web/templates/_pg_quota_gauges.html").read_text(encoding="utf-8")
        self.assertNotIn("pg-quota-banner-ico", macro)
        self.assertIn("flash-copy", macro)
        self.assertIn("is-exhausted", macro)


if __name__ == "__main__":
    unittest.main()
