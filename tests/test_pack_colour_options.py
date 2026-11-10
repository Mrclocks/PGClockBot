"""A pack editor that starts with no packs still carries every button colour for the rows added in the browser."""

from __future__ import annotations

import os
import re
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.api.app import templates
from app.services.button_styles import ITEM_BUTTON_STYLE_OPTIONS


def colour_list(html: str) -> list[str]:
    block = html.split("<template data-pack-style-options>", 1)[1].split("</template>", 1)[0]
    return re.findall(r'<option value="([^"]*)"', block)


def render(name: str, **ctx) -> str:
    return templates.env.get_template(name).render(pack_style_options=ITEM_BUTTON_STYLE_OPTIONS, packs_hidden=False, **ctx)


class EmptyPackEditorColourTests(unittest.TestCase):
    expected = [value for value, _label, _tone in ITEM_BUTTON_STYLE_OPTIONS]

    def test_user_plan_editor_without_packs_lists_every_colour(self) -> None:
        html = render("_user_plan_addon_packs_editor.html", plan_vol_packs=[], plan_dur_packs=[])
        self.assertNotIn('class="ds-list-row extra-pack-row"', html)
        self.assertEqual(colour_list(html), self.expected)

    def test_reseller_plan_editor_without_packs_lists_every_colour(self) -> None:
        html = render("_reseller_extra_packs_editor.html", extra_gb_packs=[], extra_user_packs=[])
        self.assertEqual(colour_list(html), self.expected)

    def test_the_default_colour_is_named_as_in_the_rows(self) -> None:
        html = render("_user_plan_addon_packs_editor.html", plan_vol_packs=[], plan_dur_packs=[])
        block = html.split("<template data-pack-style-options>", 1)[1].split("</template>", 1)[0]
        self.assertIn('value="inherit" data-tone="default">پیش‌فرض</option>', block)


if __name__ == "__main__":
    unittest.main()
