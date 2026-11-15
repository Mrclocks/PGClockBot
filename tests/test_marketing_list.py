"""The marketing area opens on a list of exactly the sections a role may open (DESIGN_SYSTEM.md §6-2)."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from fastapi import FastAPI, HTTPException

from app.api.marketing_pages import register_marketing_pages
from app.services.marketing_nav import (
    SECTION_DESCRIPTIONS,
    SECTION_ICONS,
    marketing_hub_sections,
    marketing_sections,
)
from app.services.panel_nav import MARKETING_LIST_HREF, build_sidebar, viewer_from_context

OWNER = {"role": "admin", "permissions": [], "org_principal_id": 1, "org_depth": 0, "org_parent_id": None}
SHOP_DISCOUNTS_ONLY = {"role": "reseller", "permissions": ["catalog_discounts"], "bot_user_id": 5}
SHOP_NOTHING = {"role": "reseller", "permissions": [], "bot_user_id": 6}


def hrefs(sections) -> list[str]:
    return [row.href for section in sections for row in section.rows]


class MarketingListServiceTests(unittest.TestCase):
    def test_owner_rows_are_the_owner_sections_with_description_and_icon(self) -> None:
        sections = marketing_sections(OWNER)
        hub = marketing_hub_sections(sections)
        self.assertEqual(sorted(hrefs(hub)), sorted(s.href for s in sections))
        for section in hub:
            for row in section.rows:
                self.assertTrue(row.description and row.icon, row.label)

    def test_a_shop_sees_only_what_its_permissions_open(self) -> None:
        self.assertEqual(hrefs(marketing_hub_sections(marketing_sections(SHOP_DISCOUNTS_ONLY))), ["/catalog-discounts"])
        self.assertEqual(marketing_hub_sections(marketing_sections(SHOP_NOTHING)), ())

    def test_every_description_and_icon_belongs_to_a_section(self) -> None:
        import app.services.marketing_nav as nav

        keys = {value.key for value in vars(nav).values() if isinstance(value, nav.MarketingSection)}
        keys |= {section.key for section in nav._CLUB_SECTIONS}
        self.assertEqual(set(SECTION_DESCRIPTIONS), keys)
        self.assertEqual(set(SECTION_ICONS), keys)

    def test_the_sidebar_entry_opens_the_list_and_stays_active_on_a_section(self) -> None:
        viewer = viewer_from_context(OWNER, {}, is_owner=True, marketing_sections=marketing_sections(OWNER))
        for path in (MARKETING_LIST_HREF, "/catalog-discounts"):
            items = {i.key: i for s in build_sidebar(viewer, path=path) for i in s.items}
            self.assertEqual(items["marketing"].href, MARKETING_LIST_HREF)
            self.assertTrue(items["marketing"].active, path)


class MarketingListRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app.api.app import render

        self.staff: dict | None = None

        async def require_staff() -> dict:
            if self.staff is None:
                raise HTTPException(status_code=401)
            return self.staff

        app = FastAPI()
        register_marketing_pages(app, render=render, require_staff=require_staff)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://example.test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_owner_gets_the_grouped_list(self) -> None:
        self.staff = OWNER
        response = await self.client.get(MARKETING_LIST_HREF)
        self.assertEqual(response.status_code, 200)
        self.assertIn('class="settings-hub', response.text)
        self.assertIn('href="/catalog-discounts"', response.text)
        self.assertIn('href="/broadcast"', response.text)

    async def test_a_shop_gets_only_its_own_sections(self) -> None:
        self.staff = SHOP_DISCOUNTS_ONLY
        hub = (await self.client.get(MARKETING_LIST_HREF)).text.split('class="settings-hub', 1)[1].split("</main>", 1)[0]
        self.assertIn('href="/catalog-discounts"', hub)
        for other in ("/broadcast", "/loyalty", "/gift-codes", "/cashback", "/funnel"):
            self.assertNotIn(f'href="{other}', hub)

    async def test_staff_without_any_section_is_refused(self) -> None:
        self.staff = SHOP_NOTHING
        self.assertEqual((await self.client.get(MARKETING_LIST_HREF)).status_code, 403)

    async def test_a_visitor_without_a_session_is_refused(self) -> None:
        self.staff = None
        self.assertEqual((await self.client.get(MARKETING_LIST_HREF)).status_code, 401)


if __name__ == "__main__":
    unittest.main()
