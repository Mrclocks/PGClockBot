"""Form pages (broadcast, action centre, message variables) render inside the design-language scope."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from types import SimpleNamespace

from jinja2 import ChainableUndefined

from app.api.app import templates
from tests.test_message_variables_entry import OWNER, _page


def render_loose(name: str) -> str:
    """Render an edit page without its records; missing values render empty instead of failing."""
    env = templates.env.overlay(undefined=ChainableUndefined)
    request = SimpleNamespace(query_params={}, url=SimpleNamespace(path="/edit"), cookies={})
    return env.get_template(name).render(request=request, staff={"role": "admin", "username": "owner"}, path="/edit")


class FormPagesDesignTests(unittest.TestCase):
    def assert_scoped(self, html: str) -> None:
        self.assertIn('<div class="ds ds-settings">', html)
        inner = html.split('<div class="ds ds-settings">', 1)[1]
        self.assertIn("page-head", inner)

    def test_broadcast_page(self) -> None:
        self.assert_scoped(_page("broadcast.html", "/broadcast", OWNER, history=[], marketing_sections=()))

    def test_action_centre_page(self) -> None:
        self.assert_scoped(_page("inbox.html", "/inbox", OWNER, ac={"entries": []}, entries=[]))

    def test_list_pages_for_resellers_and_services(self) -> None:
        for name, path in (
            ("reseller_applications.html", "/resellers/applications"),
            ("reseller_plans.html", "/resellers/plans"),
            ("service_shop_review.html", "/services/shop-review"),
        ):
            with self.subTest(name):
                self.assert_scoped(_page(name, path, OWNER))

    def test_edit_pages(self) -> None:
        for name in ("user_edit.html", "reseller_edit.html", "plan_edit.html", "reseller_plan_edit.html"):
            with self.subTest(name):
                self.assert_scoped(render_loose(name))


if __name__ == "__main__":
    unittest.main()
