"""Settings areas open on a list of their pages and show their place on each page (DESIGN_SYSTEM.md §6-2)."""

from __future__ import annotations

import os
import re
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.services.resellers import RESELLER_SETTINGS_TABS
from app.services.settings_hub import (
    SYSTEM_DESCRIPTIONS,
    SYSTEM_ICONS,
    TAB_DESCRIPTIONS,
    TAB_ICONS,
    first_settings_href,
    hub_sections,
    settings_nav,
    settings_place,
    system_hub_sections,
    system_settings_groups,
    system_settings_nav,
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
        # Other system pages are reached from the desktop menu, never from the header above the page.
        head = re.sub(r'<nav class="settings-nav.*?</nav>', "", html, flags=re.S).split("settings-layout", 1)[0]
        self.assertNotIn('href="/settings?tab=general"', head)


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

class SettingsMenuTests(unittest.TestCase):
    """The desktop menu beside every settings page: the area's rows, the open page marked."""

    def owner_groups(self, active: str):
        return group_settings_tabs(tabs_with_message_variables(SETTINGS_TABS, OWNER), active)

    def test_a_section_of_a_merged_page_marks_that_page(self) -> None:
        nav = settings_nav(self.owner_groups("welcome"), "/settings", "welcome", area_icon="bot")
        self.assertEqual(nav.active_href, "/settings?tab=identity")
        self.assertEqual(rows(nav.sections), rows(hub_sections(self.owner_groups("welcome"), "/settings", area_icon="bot")))

    def test_an_unknown_page_marks_nothing(self) -> None:
        self.assertEqual(settings_nav(self.owner_groups("welcome"), "/settings", "nope", area_icon="bot").active_href, "")

    def test_the_system_menu_marks_security_and_the_system_pages(self) -> None:
        self.assertEqual(system_settings_nav("security").active_href, "/security")
        self.assertEqual(system_settings_nav("backup").active_href, "/settings?tab=backup")

    def test_the_desktop_opens_the_first_row_of_the_list(self) -> None:
        self.assertEqual(first_settings_href(system_hub_sections()), "/settings?tab=general")
        self.assertEqual(first_settings_href(()), "")


class SettingsMenuPageTests(unittest.TestCase):
    bot_page = SettingsPageTests.bot_page
    system_page = SettingsPageTests.system_page

    def menu(self, html: str) -> str:
        return html.split('<nav class="settings-nav', 1)[1].split("</nav>", 1)[0]

    def test_a_bot_page_has_the_menu_after_its_header_with_the_page_marked(self) -> None:
        html = self.bot_page(b"tab=welcome")
        self.assertLess(html.index('class="page-head'), html.index('<nav class="settings-nav'))
        self.assertLess(html.index('<nav class="settings-nav'), html.index('class="section-tabs settings-tabs settings-sections"'))
        menu = self.menu(html)
        self.assertEqual(menu.count('aria-current="page"'), 1)
        self.assertIn('href="/settings?tab=identity" aria-current="page"', menu)
        self.assertIn('href="/message-variables"', menu)
        self.assertNotIn("settings-open-first", html)
        self.assertNotIn("data-settings-first", html)

    def test_the_list_page_has_no_menu_and_opens_its_first_page_on_a_desktop(self) -> None:
        html = self.bot_page()
        self.assertNotIn('<nav class="settings-nav', html)
        first_row = re.search(r'class="hub-row" href="([^"]+)"', html).group(1)
        self.assertIn(f'data-settings-first="{first_row}"', html)
        self.assertLess(html.index("data-settings-first"), html.index('class="settings-hub'))

    def test_the_system_list_opens_general_and_a_system_page_marks_itself(self) -> None:
        self.assertIn('data-settings-first="/settings?tab=general"', self.system_page(b"tab=system", "general"))
        menu = self.menu(self.system_page(b"tab=backup", "backup"))
        self.assertIn('href="/settings?tab=backup" aria-current="page"', menu)
        self.assertIn('href="/security"', menu)
        self.assertIn("settings-nav--neutral", self.system_page(b"tab=backup", "backup"))

    def test_a_shop_menu_stays_in_the_shop_area(self) -> None:
        tabs = tabs_with_message_variables(RESELLER_SETTINGS_TABS, SHOP_STAFF)
        html = _page("shop_settings.html", "/shop-settings", SHOP_STAFF, query=b"tab=welcome", tabs=RESELLER_SETTINGS_TABS,
                     tab="welcome", tab_nav=group_settings_tabs(tabs, "welcome"), shop_mode=True, settings_base="/shop-settings")
        hrefs = re.findall(r'href="([^"]+)"', self.menu(html))
        self.assertTrue(hrefs)
        self.assertTrue(all(h.startswith("/shop-settings?tab=") or h == "/message-variables" for h in hrefs), hrefs)


if __name__ == "__main__":
    unittest.main()
