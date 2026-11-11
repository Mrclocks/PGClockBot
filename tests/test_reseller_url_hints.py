"""The representative settings show each panel address once, with its default/custom badge first."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from tests.test_message_variables_entry import OWNER, _page

DEFAULT_PANEL = "https://panel-default.example:9000"
CUSTOM_PANEL = "https://panel-custom.example"
DEFAULT_PG = "https://pg-default.example/dashboard"


def render(*, custom_panel: str = "") -> str:
    html = _page(
        "resellers.html", "/resellers", OWNER,
        owner_view=True, tab="list",
        tabs=[{"id": "list", "href": "/resellers", "label": "لیست"}],
        show_reseller_apply="1",
        default_panel_url=DEFAULT_PANEL, default_pg_panel_url=DEFAULT_PG,
        custom_panel_url=custom_panel, custom_pg_panel_url="",
        panel_url=custom_panel or DEFAULT_PANEL, pg_panel_url=DEFAULT_PG,
        using_custom_panel_url=bool(custom_panel), using_custom_pg_panel_url=False,
        resellers=[], rows=[], items=[],
    )
    return html.split('id="reseller-urls-form"', 1)[1].split("</form>", 1)[0]


def hint(form: str, field: str) -> str:
    return form.split(f'name="{field}"', 1)[1].split("<small", 1)[1].split("</small>", 1)[0]


class ResellerUrlHintTests(unittest.TestCase):
    def test_a_default_address_is_shown_once_after_its_badge(self) -> None:
        panel = hint(render(), "reseller_panel_base_url")
        self.assertEqual(panel.count(DEFAULT_PANEL), 1)
        self.assertLess(panel.index("پیش‌فرض"), panel.index(DEFAULT_PANEL))

    def test_a_custom_address_also_names_the_default(self) -> None:
        panel = hint(render(custom_panel=CUSTOM_PANEL), "reseller_panel_base_url")
        self.assertLess(panel.index("اختصاصی"), panel.index(CUSTOM_PANEL))
        self.assertIn(f"<code dir=\"ltr\">{DEFAULT_PANEL}</code>", panel)

    def test_the_pasarguard_address_follows_the_same_layout(self) -> None:
        pg = hint(render(), "reseller_pg_panel_base_url")
        self.assertEqual(pg.count(DEFAULT_PG), 1)
        self.assertIn('class="badge pending">پیش‌فرض', pg)


if __name__ == "__main__":
    unittest.main()
