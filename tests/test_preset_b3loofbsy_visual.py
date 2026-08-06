"""Guards for shadcn preset b3loOfbsY visual mapping (luma/zinc/orange)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
BASE = (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8")
JS = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")


class PresetB3loOfbsYTests(unittest.TestCase):
    def test_radius_default_0625rem(self):
        self.assertIn("--radius: 0.625rem", CSS)

    def test_vazirmatn_kept(self):
        self.assertIn('"Vazirmatn"', CSS)
        self.assertIn("Vazirmatn", BASE)
        # Font stack must stay Vazirmatn — no Inter family token
        self.assertIn('--font: "Vazirmatn", sans-serif', CSS)
        self.assertNotIn("--font: \"Inter", CSS)
        self.assertNotIn("--font-sans: 'Inter", CSS)

    def test_dark_primary_is_orange_oklch(self):
        root = CSS.split(":root {", 1)[1].split("\n}", 1)[0]
        self.assertIn("--primary: oklch(0.47 0.157 37.304)", root)
        self.assertIn("--brand: oklch(0.705 0.213 47.604)", root)
        self.assertNotIn("--primary: #3b82f6", root)
        self.assertNotIn("--primary: #2563eb", root)

    def test_light_primary_is_orange_oklch(self):
        light = CSS.split('html[data-theme="light"] {', 1)[1].split("\n}", 1)[0]
        self.assertIn("--primary: oklch(0.553 0.195 38.402)", light)
        self.assertIn("--brand: oklch(0.646 0.222 41.116)", light)
        self.assertNotIn("--primary: #2563eb", light)

    def test_sidebar_translucent(self):
        side = re.search(r"(?ms)^\.side\s*\{([^}]+)\}", CSS)
        self.assertIsNotNone(side)
        body = side.group(1)
        self.assertIn("backdrop-filter: blur(16px)", body)
        self.assertIn("var(--sidebar)", body)

    def test_buttons_use_brand_not_blue(self):
        btn = re.search(r"(?ms)^\.btn, a\.btn, \.btn-sm, a\.btn-sm\s*\{([^}]+)\}", CSS)
        self.assertIsNotNone(btn)
        body = btn.group(1)
        self.assertIn("var(--brand-fg)", body)
        self.assertNotIn("59, 130, 246", body)
        self.assertNotIn("info-fg", body)

    def test_no_blue_pg_chrome_hex(self):
        # Semantic --info may keep blue; chrome sections must not.
        self.assertNotIn("#3d8fd1", CSS)
        self.assertNotIn("#8ec5ef", CSS)
        self.assertNotIn("#1e3a8a", CSS)
        self.assertIn("var(--section-pg)", CSS)
        self.assertIn("var(--section-bot)", CSS)

    def test_theme_color_matches_surfaces(self):
        self.assertIn('content="#09090b"', BASE)
        self.assertIn('content="#ffffff"', BASE)
        self.assertIn("'#ffffff'", JS)
        self.assertNotIn("'#fafafa'", JS)

    def test_control_fs_still_16(self):
        self.assertRegex(CSS, r"--control-fs:\s*16px")


if __name__ == "__main__":
    unittest.main()
