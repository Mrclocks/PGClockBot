"""The «ابزار و پاکسازی» area: its list shows only the tools the viewer's own routes admit, every row opens,
the sidebar and the panel search follow the same rule, and an older phone-bar choice still finds the entry."""

from __future__ import annotations

import re

from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import ResellerProfile
from app.services.mobile_bar import parse_bar_order
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

ROW_HREF = re.compile(r'<a class="hub-row" href="([^"]+)"')
OWNER_TOOLS = ["/service-cleanup", "/finance?tab=orders&amp;settings=cleanup", "/trial-users?filter=non_buyers", "/tools/health", "/tools/maintenance", "/settings?tab=backup"]


class ToolsAreaHarness(RoutesHarness):
    async def get(self, session_payload: dict, path: str):
        cookie = f"session={self.signer.dumps(session_payload)}"
        return await self.client.get(path, headers={"cookie": cookie})

    async def grant_shop(self, *permissions: str) -> None:
        async with async_sessionmaker(self.engine)() as session:
            await session.execute(
                update(ResellerProfile).where(ResellerProfile.user_id == 5)
                .values(web_permissions=",".join(permissions), bot_permissions=",".join(permissions))
            )
            await session.commit()

    async def rows(self, session_payload: dict) -> list[str]:
        response = await self.get(session_payload, "/tools")
        self.assertEqual(response.status_code, 200)
        return ROW_HREF.findall(response.text)


class ToolsListTests(ToolsAreaHarness):
    async def test_the_owner_sees_every_tool_grouped_and_each_row_opens(self) -> None:
        rows = await self.rows(OWNER_SESSION)
        self.assertEqual(rows, OWNER_TOOLS)
        for href in rows:
            with self.subTest(href=href):
                response = await self.get(OWNER_SESSION, href.replace("&amp;", "&"))
                self.assertEqual(response.status_code, 200)

    async def test_a_shop_sees_only_the_tools_its_permissions_open(self) -> None:
        await self.grant_shop("dashboard", "service_cleanup")
        self.assertEqual(await self.rows(SHOP_SESSION), ["/service-cleanup"])
        response = await self.get(SHOP_SESSION, "/service-cleanup")
        self.assertEqual(response.status_code, 200)

    async def test_a_shop_with_no_tool_is_refused_and_has_no_menu_entry(self) -> None:
        await self.grant_shop("dashboard", "orders")
        self.assertEqual((await self.get(SHOP_SESSION, "/tools")).status_code, 403)
        home = await self.get(SHOP_SESSION, "/dashboard")
        self.assertNotIn('href="/tools"', home.text)

    async def test_backup_and_order_cleanup_stay_hidden_from_a_shop_without_their_rights(self) -> None:
        await self.grant_shop("dashboard", "orders", "payments", "shop_settings", "service_cleanup")
        rows = await self.rows(SHOP_SESSION)
        self.assertNotIn("/settings?tab=backup", rows)
        self.assertNotIn("/finance?tab=orders&amp;settings=cleanup", rows)
        await self.grant_shop("dashboard", "orders", "payments", "shop_settings", "orders_purge")
        self.assertEqual(await self.rows(SHOP_SESSION), ["/finance?tab=orders&amp;settings=cleanup"])


class ToolsNavigationTests(ToolsAreaHarness):
    async def test_the_sidebar_entry_opens_the_list_and_lights_on_a_tool_page(self) -> None:
        listing = await self.get(OWNER_SESSION, "/tools")
        entry = re.search(r'<a href="/tools" class="nav-item[^"]*"[^>]*>', listing.text)
        self.assertIsNotNone(entry)
        self.assertIn('aria-current="page"', entry.group(0))
        cleanup = await self.get(OWNER_SESSION, "/service-cleanup")
        entry = re.search(r'<a href="/tools" class="nav-item[^"]*"[^>]*>', cleanup.text)
        self.assertIn('aria-current="page"', entry.group(0))

    async def test_a_tool_page_leads_back_to_the_list(self) -> None:
        page = await self.get(OWNER_SESSION, "/service-cleanup")
        self.assertIn('<a class="page-back" href="/tools" aria-label="بازگشت به ابزار و پاکسازی">', page.text)

    async def test_the_panel_search_finds_a_tool_under_its_area_only_for_viewers_who_may_open_it(self) -> None:
        found = (await self.get(OWNER_SESSION, "/panel-search?q=پاکسازی سرویس")).json()["results"]
        self.assertIn({"label": "پاکسازی سرویس‌ها", "where": "ابزار و پاکسازی", "href": "/service-cleanup"}, found)
        await self.grant_shop("dashboard", "orders")
        found = (await self.get(SHOP_SESSION, "/panel-search?q=پاکسازی سرویس")).json()["results"]
        self.assertFalse([hit for hit in found if hit["href"] in ("/service-cleanup", "/tools")])

    def test_a_bar_saved_with_the_old_cleanup_entry_keeps_its_place(self) -> None:
        self.assertEqual(parse_bar_order("service_cleanup,home"), ("tools", "home"))
