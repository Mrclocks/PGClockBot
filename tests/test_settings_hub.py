"""Settings areas open on a list of their pages and show their place on each page (DESIGN_SYSTEM.md §6-2)."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.services.resellers import RESELLER_SETTINGS_TABS
from app.services.settings_hub import (
    SYSTEM_DESCRIPTIONS,
    SYSTEM_ICONS,
    TAB_DESCRIPTIONS,
    TAB_ICONS,
    hub_sections,
    settings_place,
    system_hub_sections,
    system_settings_groups,
)
from app.services.settings_tab_groups import group_settings_tabs, tabs_with_message_variables
from app.services.users import PANEL_SETTINGS_TABS, SETTINGS_TABS
from tests.test_message_variables_entry import OWNER, SHOP_STAFF, _page

SHOP_WITHOUT_SETTINGS = {"role": "reseller", "permissions": [], "bot_user_id": 5}


def rows(sections) -> list[tuple[str, str]]:
    return [(row.label, row.href) for section in sections for row in section.rows]


class HubSectionTests(unittest.TestCase):
    def test_every_bot_settings_page_has_a_row_description_and_icon(self) -> None:
        groups = group_settings_tabs(tabs_with_message_variables(SETTINGS_TABS, OWNER), "welcome")
        sections = hub_sections(groups, "/settings", area_icon="bot")
        self.assertEqual([s.label for s in sections], [g.label for g in groups])
        for section in sections:
            for row in section.rows:
                with self.subTest(row=row.label):
                    self.assertTrue(row.description)
                    self.assertTrue(row.icon)
        hrefs = [href for _label, href in rows(sections)]
        self.assertIn("/settings?tab=identity", hrefs)
        self.assertIn("/message-variables", hrefs)

    def test_rows_follow_the_base_of_the_area(self) -> None:
        groups = group_settings_tabs(RESELLER_SETTINGS_TABS, "welcome")
        hrefs = [href for _label, href in rows(hub_sections(groups, "/shop-settings", area_icon="bot"))]
        self.assertTrue(all(h.startswith("/shop-settings?tab=") for h in hrefs))
        self.assertIn("/shop-settings?tab=bot", hrefs)

    def test_a_shop_without_the_variables_permission_gets_no_variables_row(self) -> None:
        groups = group_settings_tabs(tabs_with_message_variables(RESELLER_SETTINGS_TABS, SHOP_WITHOUT_SETTINGS), "welcome")
        self.assertNotIn("/message-variables", [h for _l, h in rows(hub_sections(groups, "/shop-settings", area_icon="bot"))])

    def test_the_owner_bot_settings_list_has_no_shop_only_page(self) -> None:
        groups = group_settings_tabs(SETTINGS_TABS, "welcome")
        self.assertNotIn("/settings?tab=bot", [h for _l, h in rows(hub_sections(groups, "/settings", area_icon="bot"))])

    def test_every_described_or_iconed_key_is_a_real_tab(self) -> None:
        keys = {k for k, _ in SETTINGS_TABS} | {k for k, _ in RESELLER_SETTINGS_TABS} | {"identity", "buttons", "delivery", "variables"}
        self.assertLessEqual(set(TAB_DESCRIPTIONS), keys)
        self.assertLessEqual(set(TAB_ICONS), keys)


class SettingsPlaceTests(unittest.TestCase):
    def test_a_section_of_a_merged_tab_is_placed_under_that_tab(self) -> None:
        place = settings_place(group_settings_tabs(SETTINGS_TABS, "appearance"), "appearance")
        self.assertEqual((place.group.label, place.tab.key), ("ظاهر ربات", "identity"))

    def test_an_unknown_tab_has_no_place(self) -> None:
        self.assertIsNone(settings_place(group_settings_tabs(SETTINGS_TABS, "welcome"), "nope"))


class SettingsPageTests(unittest.TestCase):
    def bot_page(self, query: bytes = b"", tab: str = "welcome") -> str:
        tabs = tabs_with_message_variables(SETTINGS_TABS, OWNER)
        return _page("settings.html", "/settings", OWNER, query=query, tabs=SETTINGS_TABS, tab=tab,
                     tab_nav=group_settings_tabs(tabs, tab), is_panel_settings=False)

    def test_bot_settings_without_a_tab_is_the_list(self) -> None:
        html = self.bot_page()
        self.assertIn('class="settings-hub', html)
        self.assertIn('href="/settings?tab=identity"', html)
        self.assertNotIn('class="settings-layout"', html)

    def test_a_bot_settings_page_shows_its_way_back_and_no_tab_rows(self) -> None:
        html = self.bot_page(b"tab=welcome")
        self.assertNotIn('class="settings-hub', html)
        self.assertIn('href="/settings" aria-label="بازگشت به تنظیمات ربات"', html)
        self.assertNotIn("تنظیمات ربات · ظاهر ربات", html)
        self.assertNotIn("settings-tabs-groups", html)
        self.assertNotIn("settings-subtabs", html)
        self.assertIn('class="section-tabs settings-tabs settings-sections"', html)

    def test_bot_settings_pages_have_no_telegram_preview(self) -> None:
        for tab in ("menu", "welcome", "messages", "buttons", "qr", "daily_report", "appearance"):
            html = self.bot_page(f"tab={tab}".encode(), tab=tab)
            self.assertIn('class="settings-layout"', html, tab)
            self.assertNotIn("پیش‌نمایش تلگرام", html, tab)
            self.assertNotIn("data-tg-preview", html, tab)
            self.assertNotIn("tg-preview", html, tab)

    def test_shop_settings_list_offers_only_the_shop_pages(self) -> None:
        tabs = tabs_with_message_variables(RESELLER_SETTINGS_TABS, SHOP_STAFF)
        html = _page("shop_settings.html", "/shop-settings", SHOP_STAFF, tabs=RESELLER_SETTINGS_TABS, tab="welcome",
                     tab_nav=group_settings_tabs(tabs, "welcome"), shop_mode=True, settings_base="/shop-settings")
        hub = html.split('class="settings-hub', 1)[1].split("</main>", 1)[0]
        self.assertIn('href="/shop-settings?tab=bot"', hub)
        self.assertNotIn('href="/settings?tab=', hub)

    def system_page(self, query: bytes, tab: str) -> str:
        return _page("settings.html", "/settings", OWNER, query=query, tabs=[], tab=tab, is_panel_settings=True, env_values={})

    def test_system_settings_list_offers_every_system_page_and_security(self) -> None:
        html = self.system_page(b"tab=system", "general")
        hub = html.split('class="settings-hub', 1)[1].split("</main>", 1)[0]
        for key, _label in PANEL_SETTINGS_TABS:
            self.assertIn(f'href="/settings?tab={key}"', hub)
        self.assertIn('href="/security"', hub)
        self.assertNotIn('class="settings-layout"', html)

    def test_a_system_page_leads_back_to_the_system_list(self) -> None:
        html = self.system_page(b"tab=backup", "backup")
        self.assertNotIn('class="settings-hub', html)
        self.assertIn('href="/settings?tab=system" aria-label="بازگشت به تنظیمات سیستم"', html)
        self.assertNotIn('href="/settings?tab=general"', html.split("settings-layout", 1)[0])


class SystemSettingsListTests(unittest.TestCase):
    def test_rows_follow_the_system_tabs_then_security(self) -> None:
        hrefs = [href for _label, href in rows(system_hub_sections())]
        self.assertEqual(hrefs, [f"/settings?tab={key}" for key, _ in PANEL_SETTINGS_TABS] + ["/security"])
        for section in system_hub_sections():
            for row in section.rows:
                self.assertTrue(row.description and row.icon, row.label)

    def test_every_system_description_belongs_to_a_system_page(self) -> None:
        keys = {k for k, _ in PANEL_SETTINGS_TABS} | {"security"}
        self.assertLessEqual(set(SYSTEM_DESCRIPTIONS) | set(SYSTEM_ICONS), keys)

    def test_security_is_placed_in_the_system_area(self) -> None:
        place = settings_place(system_settings_groups("security"), "security")
        self.assertEqual(place.tab.href, "/security")

if __name__ == "__main__":
    unittest.main()
