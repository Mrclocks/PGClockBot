"""The «مشتری و سرویس» settings area: its own list, menu and sidebar entry for the owner and a shop, holding the
quick links, limits, delivery and customer-service pages, which no longer sit in the bot settings list."""

from __future__ import annotations

import re

from tests.test_tools_area import ToolsAreaHarness
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION

HUB_ROW = re.compile(r'<a class="hub-row" href="([^"]+)"')
MENU_ROW = re.compile(r'<a class="settings-nav-item" href="([^"]+)"')
CUSTOMER_PAGES = ["links", "limits", "delivery", "customer_services"]


def hrefs(base: str, keys: list[str]) -> list[str]:
    return [f"{base}?tab={key}" for key in keys]


class CustomerAreaListTests(ToolsAreaHarness):
    async def test_the_owner_list_holds_only_the_customer_and_service_pages_and_each_opens(self) -> None:
        listing = await self.get(OWNER_SESSION, "/settings?tab=customer")
        self.assertEqual(listing.status_code, 200)
        self.assertIn("<h1>مشتری و سرویس</h1>", listing.text)
        self.assertEqual(HUB_ROW.findall(listing.text), hrefs("/settings", CUSTOMER_PAGES))
        for href in HUB_ROW.findall(listing.text):
            with self.subTest(href=href):
                self.assertEqual((await self.get(OWNER_SESSION, href)).status_code, 200)

    async def test_the_bot_settings_list_no_longer_holds_them(self) -> None:
        listing = await self.get(OWNER_SESSION, "/settings")
        rows = HUB_ROW.findall(listing.text)
        self.assertIn("/settings?tab=identity", rows)
        for href in hrefs("/settings", [*CUSTOMER_PAGES, "naming", "service_import"]):
            self.assertNotIn(href, rows)

    async def test_a_shop_with_the_settings_right_gets_its_own_customer_list(self) -> None:
        await self.grant_shop("dashboard", "shop_settings")
        listing = await self.get(SHOP_SESSION, "/shop-settings?tab=customer")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(HUB_ROW.findall(listing.text), hrefs("/shop-settings", CUSTOMER_PAGES))
        self.assertIn('href="/shop-settings?tab=customer"', listing.text)
        self.assertNotIn('href="/settings?tab=customer"', listing.text)

    async def test_a_shop_without_the_settings_right_neither_sees_nor_opens_it(self) -> None:
        await self.grant_shop("dashboard", "orders")
        home = await self.get(SHOP_SESSION, "/dashboard")
        self.assertNotIn("/shop-settings?tab=customer", home.text)
        listing = await self.get(SHOP_SESSION, "/shop-settings?tab=customer")
        self.assertNotEqual(listing.status_code, 200)

    async def test_a_shop_is_sent_away_from_the_owner_area(self) -> None:
        await self.grant_shop("dashboard", "shop_settings")
        response = await self.get(SHOP_SESSION, "/settings?tab=customer")
        self.assertNotEqual(response.status_code, 200)


class CustomerAreaPageTests(ToolsAreaHarness):
    async def test_a_customer_page_leads_back_to_its_list_and_its_menu_stays_in_the_area(self) -> None:
        for base, session in (("/settings", OWNER_SESSION), ("/shop-settings", SHOP_SESSION)):
            with self.subTest(base=base):
                await self.grant_shop("dashboard", "shop_settings")
                page = await self.get(session, f"{base}?tab=limits")
                self.assertEqual(page.status_code, 200)
                self.assertIn(f'<a class="page-back" href="{base}?tab=customer"', page.text)
                self.assertEqual(MENU_ROW.findall(page.text), hrefs(base, CUSTOMER_PAGES))
                self.assertIn("<title>مشتری و سرویس", page.text)

    async def test_a_bot_page_keeps_the_bot_area_menu(self) -> None:
        page = await self.get(OWNER_SESSION, "/settings?tab=messages")
        self.assertIn('<a class="page-back" href="/settings"', page.text)
        menu = MENU_ROW.findall(page.text)
        self.assertIn("/settings?tab=messages", menu)
        for href in hrefs("/settings", CUSTOMER_PAGES):
            self.assertNotIn(href, menu)

    async def test_a_merged_page_keeps_its_sections_inside_the_area(self) -> None:
        page = await self.get(OWNER_SESSION, "/settings?tab=qr")
        self.assertIn('<a class="page-back" href="/settings?tab=customer"', page.text)
        for key in ("naming", "qr", "guides"):
            self.assertIn(f'href="/settings?tab={key}"', page.text)

    async def test_the_sidebar_lights_the_customer_entry_on_its_pages_only(self) -> None:
        entry = re.compile(r'<a href="/settings\?tab=customer" class="nav-item[^"]*"[^>]*>')
        customer = entry.search((await self.get(OWNER_SESSION, "/settings?tab=links")).text)
        self.assertIn('aria-current="page"', customer.group(0))
        bot = entry.search((await self.get(OWNER_SESSION, "/settings?tab=messages")).text)
        self.assertNotIn('aria-current="page"', bot.group(0))

    async def test_the_panel_search_names_the_new_area(self) -> None:
        found = (await self.get(OWNER_SESSION, "/panel-search?q=لینک‌های سریع")).json()["results"]
        self.assertIn({"label": "لینک‌های سریع", "where": "مشتری و سرویس › مشتری", "href": "/settings?tab=links"}, found)
        await self.grant_shop("dashboard", "orders")
        found = (await self.get(SHOP_SESSION, "/panel-search?q=لینک‌های سریع")).json()["results"]
        self.assertFalse([hit for hit in found if "settings" in hit["href"]])
