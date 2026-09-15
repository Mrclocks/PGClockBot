"""Phase 2 — web panel IA: stacked accordion sections + Persian chrome."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "app/web/templates/base.html"
CSS = ROOT / "app/web/static/panel-nav-modes.css"
JS = ROOT / "app/web/static/panel.js"
PRINCIPALS = ROOT / "app/web/templates/principals.html"


class Phase2NavAccordionTests(unittest.TestCase):
    def test_no_work_mode_tabs(self):
        html = BASE.read_text(encoding="utf-8")
        self.assertNotIn("data-nav-modes", html)
        self.assertNotIn("data-nav-mode-tab", html)
        self.assertNotIn("nav-mode-btn", html)
        self.assertNotIn(">سیستم</button>", html)
        self.assertNotIn(">فروش</button>", html)
        self.assertNotIn('role="tablist"', html)
        self.assertNotIn('role="tabpanel"', html)

    def test_all_sections_stacked_with_mode_keys(self):
        html = BASE.read_text(encoding="utf-8")
        self.assertIn('data-nav-mode="system"', html)
        self.assertIn('data-nav-mode="shop"', html)
        self.assertIn('data-nav-mode="pg"', html)
        self.assertNotIn('data-nav-mode="help"', html)
        # Keep established section labels for IA continuity
        self.assertIn("پنل ربات", html)
        self.assertIn("پنل پاسارگارد", html)
        self.assertIn("وب پنل", html)
        # Help is a bottom chip (outside accordion), styled like active tags
        self.assertIn("side-help-btn", html)
        self.assertIn('href="/help/"', html)

    def test_sections_are_collapsible(self):
        html = BASE.read_text(encoding="utf-8")
        self.assertIn("nav-label-toggle", html)
        self.assertIn('data-nav-collapse="system"', html)
        self.assertIn('data-nav-collapse="shop"', html)
        self.assertIn('data-nav-collapse="pg"', html)
        self.assertNotIn('data-nav-collapse="help"', html)
        self.assertIn("nav-empty", html)

    def test_additive_css_and_js_wired(self):
        html = BASE.read_text(encoding="utf-8")
        self.assertIn("/static/panel-nav-modes.css?v={{ app_version }}", html)
        self.assertTrue(CSS.is_file())
        css = CSS.read_text(encoding="utf-8")
        self.assertNotIn(".nav-modes", css)
        self.assertNotIn("data-active-mode", css)
        self.assertIn(".nav-section.is-collapsed", css)
        self.assertIn(".nav-label-toggle", css)
        js = JS.read_text(encoding="utf-8")
        self.assertIn("initNavWorkModes", js)
        self.assertIn("activeSectionKey", js)
        self.assertIn("setOpenSection", js)
        self.assertIn(".nav-item.active", js)
        self.assertNotIn("panel-nav-collapsed", js)
        self.assertNotIn("data-nav-mode-tab", js)
        self.assertNotIn("data-active-mode", js)

    def test_sections_default_collapsed_until_js(self):
        html = BASE.read_text(encoding="utf-8")
        self.assertGreaterEqual(html.count("is-collapsed"), 3)
        self.assertIn('data-nav-collapse="system" aria-expanded="false"', html)

    def test_sections_default_collapsed_until_js(self):
        html = BASE.read_text(encoding="utf-8")
        self.assertGreaterEqual(html.count("is-collapsed"), 3)
        self.assertIn('data-nav-collapse="system" aria-expanded="false"', html)

    def test_persian_pg_role_chrome(self):
        html = BASE.read_text(encoding="utf-8")
        self.assertNotIn("PG Role:", html)
        self.assertIn("نقش پاسارگارد:", html)
        principals = PRINCIPALS.read_text(encoding="utf-8")
        self.assertNotIn("PG Role:", principals)
        self.assertIn("نقش پاسارگارد:", principals)

    def test_owner_gates_remain(self):
        html = BASE.read_text(encoding="utf-8")
        self.assertIn("{% if is_owner %}", html)
        users_idx = html.find('href="/users"')
        self.assertGreater(users_idx, 0)
        window = html[max(0, users_idx - 120) : users_idx]
        self.assertIn("is_owner", window)


if __name__ == "__main__":
    unittest.main()
