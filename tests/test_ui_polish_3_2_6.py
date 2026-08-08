"""UI polish 3.2.6 — footer, sidebar theme gap, table actions, PG overview, selects."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "app/web/static/panel.css"
JS = ROOT / "app/web/static/panel.js"
MACROS = ROOT / "app/web/templates/macros.html"
PG_HOME = ROOT / "app/web/templates/pg_home.html"
PG_PAGES = ROOT / "app/api/pg_pages.py"


class FooterScrollFixTests(unittest.TestCase):
    def test_main_body_is_scrollport_under_pinned_footer(self):
        css = CSS.read_text(encoding="utf-8")
        body = css.split(".main-body {\n", 1)[1].split("}", 1)[0]
        self.assertIn("flex: 1 1 auto;", body)
        self.assertIn("min-height: 0;", body)
        self.assertIn("overflow-y: auto;", body)
        mobile = css.split("@media (max-width: 900px)", 1)[1]
        self.assertIn("overflow-y: auto;", mobile.split(".main-body", 1)[1].split("}", 1)[0])
        foot = css.split(".site-footer {\n", 1)[1].split("}", 1)[0]
        self.assertIn("margin-top: 0;", foot)


class SidebarThemeGapTests(unittest.TestCase):
    def test_theme_separator_equal_air(self):
        css = CSS.read_text(encoding="utf-8")
        theme = css.split(".side-theme {\n", 1)[1].split("}", 1)[0]
        self.assertIn("margin-bottom: var(--space-1);", theme)
        self.assertIn("padding: 0 0 var(--space-2);", theme)
        self.assertIn(".side-nav > .nav-section:first-child", css)
        first = css.split(".side-nav > .nav-section:first-child", 1)[1].split("}", 1)[0]
        self.assertIn("margin-top: 0;", first)


class TableActionsTests(unittest.TestCase):
    def test_operations_label_toggle_in_macro(self):
        src = MACROS.read_text(encoding="utf-8")
        self.assertIn("row-actions-toggle--icon", src)
        self.assertIn("row-actions-toggle--label", src)
        self.assertIn(">عملیات<", src)

    def test_inline_actions_nowrap(self):
        css = CSS.read_text(encoding="utf-8")
        menu = re.search(r"(?ms)^\.row-actions-menu\s*\{([^}]+)\}", css)
        self.assertIsNotNone(menu)
        self.assertIn("flex-wrap: nowrap", menu.group(1))
        self.assertIn(".table-wrap.force-kebab .row-actions-toggle--label", css)
        self.assertIn("عملیات", MACROS.read_text(encoding="utf-8"))

    def test_force_kebab_checks_action_height(self):
        js = JS.read_text(encoding="utf-8")
        self.assertIn("offsetHeight > 44", js)
        self.assertIn("items.length > 1", js)
        self.assertIn("scrollWidth > menu.clientWidth", js)


class CustomSelectTests(unittest.TestCase):
    def test_ui_select_styles_and_js(self):
        css = CSS.read_text(encoding="utf-8")
        js = JS.read_text(encoding="utf-8")
        self.assertIn(".ui-select-toggle", css)
        self.assertIn(".ui-select-menu", css)
        self.assertIn("enhanceSelect", js)
        self.assertIn("closeUiSelects", js)
        self.assertIn("ui-select-native", js)


class PgOverviewMergeTests(unittest.TestCase):
    def test_admin_overview_is_single_home_panel(self):
        src = PG_HOME.read_text(encoding="utf-8")
        # Admin branch: one merged panel, no separate top stats-grid
        admin = src.split("{% else %}", 1)[1]
        self.assertIn("home-panel home-panel-pg", admin)
        self.assertIn("آمار پنل", admin)
        self.assertIn("دسترسی سریع", admin)
        self.assertNotIn('class="stats-grid"', admin)
        self.assertNotIn("pg-stats-panel", admin)
        self.assertIn("counts.users", admin)
        self.assertIn("stats_rows", admin)

    def test_duplicate_count_keys_filtered(self):
        src = PG_PAGES.read_text(encoding="utf-8")
        self.assertIn("_count_dup_keys", src)
        self.assertIn('"total_user"', src)
        self.assertIn('"admins_total"', src)


class PageTitleGapBumpTests(unittest.TestCase):
    def test_page_title_gap_is_24(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("--page-title-gap: var(--space-3);", css)
        self.assertIn("--space-3: 24px;", css)
        self.assertNotIn("--page-title-gap: 16px;", css)


if __name__ == "__main__":
    unittest.main()
