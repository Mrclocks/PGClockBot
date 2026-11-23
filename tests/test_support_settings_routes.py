"""What the Owner's notification and support-contact forms save, refuse and delete, and that a
shop cannot use them to change the platform's settings."""

from __future__ import annotations

import json

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Setting
from app.services.support_contacts import SETTING_KEY, parse_support_contacts
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

CONTACTS_PAGE = "/tickets?supports=1&stab=contacts"


class SupportSettingsHarness(RoutesHarness):
    async def post(self, path: str, form: dict[str, str], *, session_payload: dict = OWNER_SESSION) -> httpx.Response:
        cookie = f"session={self.signer.dumps(session_payload)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.post(path, headers=headers, data={**form, "csrf_token": "token"})

    async def platform_setting(self, key: str) -> str | None:
        """The platform-wide value; shop values are stored in another table."""
        async with async_sessionmaker(self.engine)() as session:
            return await session.scalar(select(Setting.value).where(Setting.key == key))

    async def platform_contacts(self) -> list[dict]:
        return parse_support_contacts(await self.platform_setting(SETTING_KEY))


class SupportContactsTests(SupportSettingsHarness):
    async def test_the_bulk_editor_saves_the_list_it_sends(self):
        rows = [{"title": "فروش", "telegram": "@sales_desk", "sort": 1},
                {"title": "فنی", "telegram": "@tech_desk", "sort": 0}]
        response = await self.post("/supports/save", {"support_contacts": json.dumps(rows)})
        self.assertEqual(response.headers["location"], f"{CONTACTS_PAGE}&saved=1")
        contacts = await self.platform_contacts()
        self.assertEqual([(c["title"], c["telegram"]) for c in contacts],
                         [("فنی", "@tech_desk"), ("فروش", "@sales_desk")])

    async def test_a_row_without_a_title_is_refused_and_nothing_is_saved(self):
        rows = [{"title": "", "telegram": "@sales_desk"}]
        response = await self.post("/supports/save", {"support_contacts": json.dumps(rows)})
        self.assertIn(f"{CONTACTS_PAGE}&err=", response.headers["location"])
        self.assertEqual(await self.platform_contacts(), [])

    async def test_a_single_row_form_adds_a_contact_and_editing_it_without_the_switch_disables_it(self):
        await self.post("/supports/save", {"title": "فروش", "telegram": "@sales_desk", "enabled": "on"})
        [added] = await self.platform_contacts()
        self.assertTrue(added["enabled"])
        await self.post("/supports/save", {"id": added["id"], "title": "فروش", "telegram": "@sales_desk"})
        [edited] = await self.platform_contacts()
        self.assertEqual(edited["id"], added["id"])
        self.assertFalse(edited["enabled"])

    async def test_editing_a_contact_that_does_not_exist_is_refused(self):
        response = await self.post("/supports/save", {"id": "missing", "title": "فروش", "telegram": "@sales_desk"})
        self.assertIn(f"{CONTACTS_PAGE}&err=", response.headers["location"])
        self.assertEqual(await self.platform_contacts(), [])

    async def test_delete_removes_only_the_chosen_contact(self):
        for title, handle in (("فروش", "@sales_desk"), ("فنی", "@tech_desk")):
            await self.post("/supports/save", {"title": title, "telegram": handle, "enabled": "on"})
        sales = next(c for c in await self.platform_contacts() if c["title"] == "فروش")
        response = await self.post("/supports/delete", {"id": sales["id"]})
        self.assertEqual(response.headers["location"], f"{CONTACTS_PAGE}&saved=1")
        self.assertEqual([c["title"] for c in await self.platform_contacts()], ["فنی"])

    async def test_a_shop_cannot_change_the_platform_support_contacts(self):
        response = await self.post("/supports/save", {"title": "فروش", "telegram": "@sales_desk", "enabled": "on"},
                                   session_payload=SHOP_SESSION)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/home"))
        self.assertEqual(await self.platform_contacts(), [])


class NotificationPrefsTests(SupportSettingsHarness):
    async def test_ticked_preferences_are_saved_on_and_the_rest_off(self):
        response = await self.post("/notifications", {"s_notify_panel_update": "on"})
        self.assertEqual(response.headers["location"], "/settings?tab=notifications&saved=1")
        self.assertEqual(await self.platform_setting("notify_panel_update"), "1")
        await self.post("/notifications", {})
        self.assertEqual(await self.platform_setting("notify_panel_update"), "0")

    async def test_a_shop_cannot_change_the_owner_notification_preferences(self):
        response = await self.post("/notifications", {"s_notify_panel_update": "on"}, session_payload=SHOP_SESSION)
        self.assertEqual(response.headers["location"], "/home")
        self.assertIsNone(await self.platform_setting("notify_panel_update"))
