"""An update writes the new templates before the panel restarts, so for a while the previous release's Python
renders them. base.html must call panel_sidebar with the six arguments every release since 0.7.4 accepts;
the tools menu still reaches the sidebar through the page context."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from app.api import app as app_module
from app.services.panel_nav import sidebar_for_template
from tests.test_update_owner_only import OWNER_SESSION, RoutesHarness


def sidebar_of_0_7_4(staff: Any, identity: Any, is_owner: bool, marketing_sections: Any, path: str, tab: str) -> Any:
    """panel_sidebar as 0.7.4 registered it: exactly six arguments, no keyword for the tools menu."""
    return sidebar_for_template(staff, identity, is_owner, marketing_sections, path, tab)


class TemplateUpdateWindowTests(RoutesHarness):
    async def page(self, path: str) -> Any:
        cookie = f"session={self.signer.dumps(OWNER_SESSION)}"
        return await self.client.get(path, headers={"cookie": cookie})

    async def test_pages_render_with_the_previous_releases_sidebar(self) -> None:
        with patch.dict(app_module.templates.env.globals, {"panel_sidebar": sidebar_of_0_7_4}):
            for path in ("/home", "/inbox", "/settings", "/tools"):
                with self.subTest(path=path):
                    self.assertEqual((await self.page(path)).status_code, 200)

    async def test_the_tools_menu_still_reaches_the_sidebar(self) -> None:
        page = (await self.page("/tools")).text
        self.assertIn('href="/tools"', page)
        self.assertIn('aria-current="page"', page)
