"""Resellers/users risk-tag filter is a dropdown under the list title — not pills."""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

ROOT = Path(__file__).resolve().parents[1]


def _render_list_page(template_name: str, path: str, **extra: object) -> str:
    from app.api.app import templates

    request = MagicMock()
    request.url.path = path
    request.query_params = {}
    ctx = {
        "request": request,
        "staff": {"role": "admin", "permissions": ["users", "resellers"]},
        "q": "",
        "color_filter": "",
        "color_tags": [{"key": "red", "hex": "#ff0000", "title_fa": "قرمز"}],
        "filter_href": lambda **_kw: path,
    }
    ctx.update(extra)
    return templates.env.get_template(template_name).render(**ctx)


def _search_form(html: str) -> str:
    match = re.search(
        r'<form[^>]*class="search-bar search-bar--with-tag".*?</form>', html, re.S
    )
    assert match, "search form with the tag filter is missing"
    return match.group(0)


class ColorTagFilterDropdownTests(unittest.TestCase):
    def test_resellers_uses_dropdown_in_search_row(self):
        html = _render_list_page("resellers.html", "/resellers")
        form = _search_form(html)
        self.assertIn("data-color-tag-nav", form)
        self.assertIn("color-tag-select", form)
        self.assertIn("قرمز", form)
        self.assertNotIn("color-tag-filters", html)
        # Full-height search row control — not the compact select-sm tier.
        self.assertNotIn("select-sm", form)

    def test_users_uses_dropdown_not_pills(self):
        html = _render_list_page("users.html", "/users", flt="all")
        form = _search_form(html)
        self.assertIn("data-color-tag-nav", form)
        self.assertIn("قرمز", form)
        self.assertNotIn("color-tag-filters", html)
        self.assertNotIn("select-sm", form)

    def test_tag_filter_hidden_without_tags(self):
        html = _render_list_page("users.html", "/users", flt="all", color_tags=[])
        self.assertNotIn("data-color-tag-nav", html)

    def test_js_navigates_on_change(self):
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertIn("data-color-tag-nav", js)
        self.assertIn("window.location.assign", js)

    def test_tag_filter_ds_override_beats_compact_select(self):
        """Only panel-ds fights .ui-select-sm !important; base wrap stays btn-h + capsule."""
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        ds = (ROOT / "app/web/static/panel-ds.css").read_text(encoding="utf-8")
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        wrap_block = css.split(".color-tag-select-wrap {", 1)[1].split("}", 1)[0]
        self.assertIn("height: var(--btn-h)", wrap_block)
        self.assertIn("border-radius: var(--control-radius)", wrap_block)
        self.assertNotIn("!important", wrap_block)
        override = ds.split(
            "/* Tag filter in the search row must match the adjacent search .btn", 1
        )[1].split("@media", 1)[0]
        self.assertIn("height: var(--btn-h) !important", override)
        self.assertIn("border-radius: var(--control-radius) !important", override)
        self.assertIn("forceFullHeight", js)
        self.assertIn(
            "color-tag-select",
            js.split("forceFullHeight", 1)[1].split("useSm", 1)[0],
        )
        self.assertIn("search-bar.search-bar--with-tag", css)


if __name__ == "__main__":
    unittest.main()
