"""Small settings tabs about one subject share one tab and show as sections inside it."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.api.app import templates
from app.services.resellers import RESELLER_SETTINGS_TABS
from app.services.settings_tab_groups import MESSAGE_VARIABLES_TAB, group_settings_tabs, resolve_merged_tab
from app.services.users import SETTINGS_TABS

MERGES = {
    "identity": ["welcome", "appearance"],
    "buttons": ["buttons", "colors"],
    "delivery": ["naming", "qr", "guides"],
}


def tabs_by_key(tabs, active: str = "welcome") -> dict:
    return {t.key: t for g in group_settings_tabs(tabs, active) for t in g.tabs}


class MergedTabTests(unittest.TestCase):
    def test_owner_and_shop_get_the_three_merged_tabs_with_their_sections(self) -> None:
        for tabs in (SETTINGS_TABS, RESELLER_SETTINGS_TABS):
            by_key = tabs_by_key(tabs)
            for merged, members in MERGES.items():
                self.assertEqual([s.key for s in by_key[merged].sections], members)
            for member in ("welcome", "appearance", "colors", "naming", "qr", "guides"):
                self.assertNotIn(member, by_key)

    def test_owner_bot_settings_shrink_to_thirteen_tabs(self) -> None:
        self.assertEqual(len(tabs_by_key([*SETTINGS_TABS, MESSAGE_VARIABLES_TAB])), 13)

    def test_merged_tab_is_active_on_any_of_its_sections(self) -> None:
        for active in ("naming", "qr", "guides"):
            groups = group_settings_tabs(SETTINGS_TABS, active)
            active_tabs = [t.key for g in groups for t in g.tabs if t.shows(active)]
            self.assertEqual(active_tabs, ["delivery"])
            self.assertEqual([g.key for g in groups if g.is_active], ["service"])

    def test_a_merge_needs_two_held_tabs(self) -> None:
        by_key = tabs_by_key([("welcome", "خوش‌آمد"), ("naming", "نام"), ("qr", "QR")])
        self.assertEqual(by_key["welcome"].sections, ())
        self.assertEqual([s.key for s in by_key["delivery"].sections], ["naming", "qr"])

    def test_merged_keys_open_their_first_section_and_others_stay(self) -> None:
        self.assertEqual(resolve_merged_tab("identity"), "welcome")
        self.assertEqual(resolve_merged_tab("delivery"), "naming")
        self.assertEqual(resolve_merged_tab("buttons"), "buttons")
        for key in ("colors", "qr", "limits", "general", "nonsense"):
            self.assertEqual(resolve_merged_tab(key), key)


class SectionStripTests(unittest.TestCase):
    def render(self, active: str, base: str = "/settings") -> str:
        tabs = SETTINGS_TABS if base == "/settings" else RESELLER_SETTINGS_TABS
        macro = templates.env.get_template("_settings_tab_nav.html").module.grouped_settings_tabs
        return str(macro(group_settings_tabs(tabs, active), active, base, "tabs"))

    def test_section_strip_shows_only_for_the_active_merged_tab(self) -> None:
        html = self.render("qr")
        self.assertEqual(html.count("settings-sections"), 1)
        self.assertIn('href="/settings?tab=qr" class="active" aria-current="page"', html)
        self.assertIn('href="/settings?tab=naming" class=""', html)
        self.assertNotIn("settings-sections", self.render("limits"))

    def test_shop_sections_stay_on_the_shop_base(self) -> None:
        html = self.render("appearance", "/shop-settings")
        self.assertIn('href="/shop-settings?tab=welcome"', html)
        self.assertNotIn('href="/settings?', html)


if __name__ == "__main__":
    unittest.main()
