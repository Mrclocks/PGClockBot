"""Settings pages render inside the design-language scope that styles their tabs and cards."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.services.resellers import RESELLER_SETTINGS_TABS
from app.services.users import SETTINGS_TABS
from tests.test_message_variables_entry import OWNER, SHOP_STAFF, _page


class SettingsPagesDesignTests(unittest.TestCase):
    def assert_scoped(self, html: str) -> None:
        body = html.split('<div class="ds ds-settings">', 1)
        self.assertEqual(len(body), 2, "page content is not in the design scope")
        self.assertIn("page-head", body[1].split("</main>", 1)[0])

    def test_bot_settings_page(self) -> None:
        self.assert_scoped(_page("settings.html", "/settings", OWNER, tabs=SETTINGS_TABS, tab="limits", is_panel_settings=False))

    def test_system_settings_page(self) -> None:
        self.assert_scoped(_page("settings.html", "/settings", OWNER, query=b"tab=general", tabs=[], tab="general",
                                 is_panel_settings=True, env_values={}))

    def test_shop_settings_page(self) -> None:
        self.assert_scoped(_page("shop_settings.html", "/shop-settings", SHOP_STAFF, tabs=RESELLER_SETTINGS_TABS,
                                 tab="limits", shop_mode=True, settings_base="/shop-settings"))


if __name__ == "__main__":
    unittest.main()
