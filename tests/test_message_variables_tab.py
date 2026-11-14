"""«متغیرهای پیام» is a tab of the bot settings, shown only to roles that can open it."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from app.api.message_variables_pages import register_message_variables_pages
from app.services.authz import authz_from_staff, can_shop
from app.services.resellers import RESELLER_SETTINGS_TABS
from app.services.settings_tab_groups import (
    MESSAGE_VARIABLES_TAB_KEY,
    can_open_message_variables,
    group_settings_tabs,
    tabs_with_message_variables,
)
from app.services.users import DEFAULT_SETTINGS, SETTINGS_TABS

OWNER = {
    "role": "admin",
    "permissions": [],
    "org_principal_id": 1,
    "org_depth": 0,
    "org_parent_id": None,
    "org_status": "active",
    "web_owner": True,
}
SHOP_WITH_RIGHT = {"role": "reseller", "permissions": ["shop_settings"], "bot_user_id": 5}
SHOP_WITHOUT_RIGHT = {"role": "reseller", "permissions": ["plans", "orders"], "bot_user_id": 5}
SHOP_NO_PERMISSIONS = {"role": "reseller", "permissions": [], "bot_user_id": 5}


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", b"example.test")],
            "client": ("127.0.0.1", 123),
            "server": ("example.test", 443),
            "state": {},
        }
    )


def _settings_page(name: str, path: str, staff: dict, **ctx) -> str:
    from app.api.app import render

    base = {"staff": staff, "values": dict(DEFAULT_SETTINGS), "groups": {}, "tab_groups": [], **ctx}
    return render(_request(path), name, base).body.decode("utf-8")


def _tab_strip(html: str) -> str:
    return html.split("settings-layout", 1)[0].split("msg-vars-jump", 1)[0]


class VisibilityMirrorsTheRouteTests(unittest.TestCase):
    def test_the_tab_follows_exactly_the_right_the_page_requires(self):
        for staff, expected in (
            (OWNER, True),
            (SHOP_WITH_RIGHT, True),
            (SHOP_WITHOUT_RIGHT, False),
            (SHOP_NO_PERMISSIONS, False),
        ):
            self.assertEqual(can_open_message_variables(staff), expected, staff)
            self.assertEqual(can_open_message_variables(staff), can_shop(authz_from_staff(staff), "shop_settings"))

    def test_a_role_without_the_right_keeps_exactly_its_tabs(self):
        self.assertEqual(tabs_with_message_variables(SETTINGS_TABS, SHOP_WITHOUT_RIGHT), list(SETTINGS_TABS))
        self.assertEqual(tabs_with_message_variables(RESELLER_SETTINGS_TABS, SHOP_NO_PERMISSIONS), list(RESELLER_SETTINGS_TABS))

    def test_the_input_list_is_never_modified(self):
        original = list(SETTINGS_TABS)
        tabs_with_message_variables(SETTINGS_TABS, OWNER)
        self.assertEqual(list(SETTINGS_TABS), original)


class GroupingTests(unittest.TestCase):
    def test_it_joins_the_texts_group_last_and_opens_its_own_page(self):
        for tabs, staff in ((SETTINGS_TABS, OWNER), (RESELLER_SETTINGS_TABS, SHOP_WITH_RIGHT)):
            groups = group_settings_tabs(tabs_with_message_variables(tabs, staff), "welcome")
            texts = next(group for group in groups if group.key == "texts")
            self.assertEqual(texts.tabs[-1].key, MESSAGE_VARIABLES_TAB_KEY)
            self.assertEqual(texts.tabs[-1].href, "/message-variables")
            self.assertEqual([t.href for t in texts.tabs[:-1]], [None] * (len(texts.tabs) - 1))

    def test_every_tab_still_appears_exactly_once_and_no_group_is_added(self):
        for tabs, staff in ((SETTINGS_TABS, OWNER), (RESELLER_SETTINGS_TABS, SHOP_WITH_RIGHT)):
            before = group_settings_tabs(tabs, "welcome")
            after = group_settings_tabs(tabs_with_message_variables(tabs, staff), "welcome")
            keys = [s.key for group in after for tab in group.tabs for s in (tab.sections or (tab,))]
            self.assertEqual(len(keys), len(set(keys)))
            self.assertEqual(sorted(keys), sorted([*(k for k, _ in tabs), MESSAGE_VARIABLES_TAB_KEY]))
            self.assertEqual([g.key for g in before], [g.key for g in after])

    def test_on_the_variables_page_the_texts_group_and_its_tab_are_active(self):
        groups = group_settings_tabs(tabs_with_message_variables(SETTINGS_TABS, OWNER), MESSAGE_VARIABLES_TAB_KEY)
        self.assertEqual([g.key for g in groups if g.is_active], ["texts"])


class SettingsPagesTests(unittest.TestCase):
    def _owner_page(self, tab: str) -> str:
        nav = group_settings_tabs(tabs_with_message_variables(SETTINGS_TABS, OWNER), tab)
        return _settings_page(
            "settings.html", "/settings", OWNER,
            tabs=SETTINGS_TABS, tab=tab, is_panel_settings=False, tab_nav=nav,
        )

    def test_the_header_button_is_gone_and_the_tab_is_in_the_texts_group(self):
        html = self._owner_page("messages")
        head = html.split("settings-layout", 1)[0]
        # The page header holds no button to the variables page (only the help, in «⋯»).
        actions = head.split("page-head-actions", 1)[-1].split("data-page-more-toggle", 1)[0]
        self.assertNotIn('href="/message-variables"', actions)
        self.assertEqual(head.count('href="/message-variables"'), 1)
        self.assertIn('href="/message-variables" class="">متغیرهای پیام</a>', head)

    def test_the_subtab_is_only_shown_inside_its_own_group(self):
        head = self._owner_page("welcome").split("settings-layout", 1)[0]
        self.assertNotIn('href="/message-variables"', head)

    def test_shop_page_has_the_tab_for_a_shop_with_the_right_and_not_without(self):
        for staff, expected in ((SHOP_WITH_RIGHT, 1), (SHOP_WITHOUT_RIGHT, 0)):
            nav = group_settings_tabs(tabs_with_message_variables(RESELLER_SETTINGS_TABS, staff), "messages")
            html = _settings_page(
                "shop_settings.html", "/shop-settings", staff,
                tabs=RESELLER_SETTINGS_TABS, tab="messages", shop_mode=True, settings_base="/shop-settings", tab_nav=nav,
            )
            head = html.split("settings-layout", 1)[0]
            self.assertEqual(head.count('href="/message-variables"'), expected, staff)
            self.assertNotIn('class="btn', head.split("page-head-actions", 1)[-1].split("data-page-more-toggle", 1)[0])

    def test_panel_settings_have_no_bot_tabs_at_all(self):
        html = _settings_page(
            "settings.html", "/settings", OWNER,
            tabs=[], tab="general", is_panel_settings=True, env_values={}, tab_nav=group_settings_tabs([], "general"),
        )
        self.assertNotIn('href="/message-variables"', html.split("settings-layout", 1)[0])


class VariablesPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.api.app import render

        self.staff: dict = {}

        def require_perm(perm: str):
            async def _dep() -> dict:
                if not can_shop(authz_from_staff(self.staff), perm):
                    raise HTTPException(status_code=403)
                return self.staff

            return _dep

        async def get_db():
            yield None

        app = FastAPI()
        register_message_variables_pages(
            app, render=render, require_perm=require_perm, require_admin=lambda: self.staff, get_db=get_db
        )
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def _get(self, staff: dict):
        self.staff.clear()
        self.staff.update(staff)
        return await self.client.get("/message-variables")

    async def test_owner_gets_the_platform_tab_strip_with_the_variables_tab_active(self):
        response = await self._get(OWNER)
        self.assertEqual(response.status_code, 200)
        strip = _tab_strip(response.text)
        self.assertIn('href="/settings?tab=identity"', strip)
        self.assertIn('href="/message-variables" class="active">متغیرهای پیام</a>', strip)
        self.assertIn('href="/settings?tab=delivery" class=""', strip)
        self.assertNotIn("/shop-settings", strip)

    async def test_shop_owner_gets_the_shop_tab_strip(self):
        response = await self._get(SHOP_WITH_RIGHT)
        self.assertEqual(response.status_code, 200)
        strip = _tab_strip(response.text)
        self.assertIn('href="/shop-settings?tab=identity"', strip)
        self.assertIn('href="/message-variables" class="active">متغیرهای پیام</a>', strip)
        self.assertNotIn('href="/settings?tab=', strip)

    async def test_a_role_without_the_right_cannot_open_the_page_the_tab_would_point_to(self):
        for staff in (SHOP_WITHOUT_RIGHT, SHOP_NO_PERMISSIONS):
            self.assertEqual((await self._get(staff)).status_code, 403, staff)

    async def test_owner_only_variables_stay_hidden_from_a_shop(self):
        owner = await self._get(OWNER)
        shop = await self._get(SHOP_WITH_RIGHT)
        self.assertGreater(owner.text.count("msg-vars-token-box"), shop.text.count("msg-vars-token-box"))


if __name__ == "__main__":
    unittest.main()
