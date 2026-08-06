"""UI polish 2.6.1 — PG counts, footer align, RAM ring, sidebar hover."""

from __future__ import annotations

import unittest
from pathlib import Path


class PgDashboardCountsTests(unittest.TestCase):
    def test_pg_home_users_admins_first(self):
        src = Path("app/web/templates/pg_home.html").read_text(encoding="utf-8")
        users_i = src.find(">کاربران</span>")
        admins_i = src.find(">ادمین‌ها</span>")
        groups_i = src.find(">گروه‌ها</span>")
        self.assertGreater(users_i, 0)
        self.assertGreater(admins_i, users_i)
        self.assertGreater(groups_i, admins_i)
        self.assertNotIn(">تمپلیت‌ها</span>", src)
        self.assertIn("counts.admins", src)
        self.assertIn("counts.users", src)

    def test_overview_fetches_admins(self):
        src = Path("app/services/home_overview.py").read_text(encoding="utf-8")
        self.assertIn("get_admins_simple", src)
        self.assertIn('"admins"', src)
        self.assertNotIn("get_user_templates_simple", src)

    def test_pg_pages_counts_admins(self):
        src = Path("app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn('counts = {"admins": 0', src)
        self.assertIn("get_admins_simple()", src)


class FooterAndMobileTests(unittest.TestCase):
    def test_site_footer_matches_side_foot_padding(self):
        css = Path("app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(".site-footer {\n  margin-top: auto;\n  padding-top: var(--page-title-gap);", css)
        self.assertIn(
            "padding: var(--page-title-gap) var(--space-4) calc(var(--page-title-gap) + var(--safe-bottom));",
            css,
        )

    def test_mobile_main_top_gap_increased(self):
        css = Path("app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(
            "padding: var(--page-title-gap) var(--space-2) calc(var(--page-title-gap) + var(--safe-bottom));",
            css,
        )


class SidebarHoverTests(unittest.TestCase):
    def test_section_hovers_use_box_tints(self):
        css = Path("app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(".nav-item-bot:hover {\n  background: color-mix(in oklch, var(--section-bot)", css)
        self.assertIn(".nav-item-pg:hover {\n  background: color-mix(in oklch, var(--section-pg)", css)


class RamRingTests(unittest.TestCase):
    def test_percent_centered_in_ring(self):
        home = Path("app/web/templates/home.html").read_text(encoding="utf-8")
        css = Path("app/web/static/panel.css").read_text(encoding="utf-8")
        # mem percent id is inside home-gauge-center (not quiet)
        center = home.split('data-metric="mem"')[1].split("home-gauge-meta")[0]
        self.assertIn('id="home-mem-val"', center)
        self.assertIn("home-gauge-center", center)
        self.assertNotIn("home-gauge-center-quiet", home)
        self.assertIn(".home-gauge-center strong", css)


if __name__ == "__main__":
    unittest.main()
