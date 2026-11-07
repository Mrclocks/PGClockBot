"""Message variables are reached from a tab of the bot settings pages, not from a sidebar item."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from starlette.requests import Request

from app.services.resellers import RESELLER_SETTINGS_TABS
from app.services.users import DEFAULT_SETTINGS, SETTINGS_TABS

OWNER = {
    "role": "admin",
    "permissions": [],
    "org_principal_id": 1,
    "org_depth": 0,
    "org_parent_id": None,
    "org_status": "active",
}
SHOP_STAFF = {"role": "reseller", "permissions": ["shop_settings"], "bot_user_id": 5}


def _request(path: str, query: bytes = b"") -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query,
            "headers": [(b"host", b"example.test")],
            "client": ("127.0.0.1", 123),
            "server": ("example.test", 443),
            "state": {},
        }
    )


def _page(name: str, path: str, staff: dict, *, query: bytes = b"", **extra) -> str:
    from app.api.app import render

    ctx = {"staff": staff, "values": dict(DEFAULT_SETTINGS), "groups": {}, "tab_groups": [], **extra}
    return render(_request(path, query), name, ctx).body.decode("utf-8")


def _sidebar(html: str) -> str:
    return html.split('data-side-nav>', 1)[1].split("</nav>", 1)[0]


class MessageVariablesEntryTests(unittest.TestCase):
    def test_owner_settings_page_links_to_the_variables_as_a_tab(self):
        from app.services.settings_tab_groups import group_settings_tabs, tabs_with_message_variables

        nav = group_settings_tabs(tabs_with_message_variables(SETTINGS_TABS, OWNER), "messages")
        html = _page(
            "settings.html", "/settings", OWNER, tabs=SETTINGS_TABS, tab="messages", is_panel_settings=False, tab_nav=nav
        )
        self.assertIn('href="/message-variables"', html.split("settings-layout", 1)[0])
        self.assertNotIn('href="/message-variables"', _sidebar(html))

    def test_panel_settings_do_not_offer_the_bot_variables(self):
        html = _page(
            "settings.html", "/settings", OWNER, query=b"tab=general", tabs=[], tab="general", is_panel_settings=True, env_values={}
        )
        self.assertNotIn('href="/message-variables"', html.split("settings-layout", 1)[0])

    def test_shop_settings_page_links_to_the_variables_as_a_tab(self):
        from app.services.settings_tab_groups import group_settings_tabs, tabs_with_message_variables

        nav = group_settings_tabs(tabs_with_message_variables(RESELLER_SETTINGS_TABS, SHOP_STAFF), "messages")
        html = _page(
            "shop_settings.html", "/shop-settings", SHOP_STAFF,
            tabs=RESELLER_SETTINGS_TABS, tab="messages", shop_mode=True, settings_base="/shop-settings", tab_nav=nav,
        )
        self.assertIn('href="/message-variables"', html.split("settings-layout", 1)[0])
        self.assertNotIn('href="/message-variables"', _sidebar(html))

    def test_variables_page_links_back_to_the_right_settings(self):
        from app.services.settings_tab_groups import (
            MESSAGE_VARIABLES_TAB_KEY, group_settings_tabs, tabs_with_message_variables,
        )

        for staff, tabs, base, other in (
            (OWNER, SETTINGS_TABS, "/settings", "/shop-settings"),
            (SHOP_STAFF, RESELLER_SETTINGS_TABS, "/shop-settings", "/settings?tab="),
        ):
            nav = group_settings_tabs(tabs_with_message_variables(tabs, staff), MESSAGE_VARIABLES_TAB_KEY)
            html = _page("message_variables.html", "/message-variables", staff, groups=[], settings_base=base, tab_nav=nav)
            head = html.split("msg-vars-stack", 1)[0].split('<main class="main">', 1)[1]
            self.assertIn(f'href="{base}?tab=welcome"', head)
            self.assertNotIn(other, head)

    def test_settings_sidebar_item_stays_active_on_the_variables_page(self):
        html = _page("message_variables.html", "/message-variables", OWNER, groups=[], settings_base="/settings")
        item = _sidebar(html).split('href="/settings"', 1)[1].split("</a>", 1)[0]
        self.assertIn("active", item)


if __name__ == "__main__":
    unittest.main()
