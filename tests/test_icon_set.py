"""One icon set: every icon macro draws from icon(), and one concept always gets the same drawing."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from jinja2 import Environment, FileSystemLoader

from app.services import panel_nav

TEMPLATES = Environment(loader=FileSystemLoader("app/web/templates"), autoescape=True)
IMPORTS = (
    '{% from "_icons.html" import icon %}{% from "macros.html" import page_ico, card_title, action_center_icon %}'
    '{% from "_nav_icons.html" import nav_icon %}{% from "_ds.html" import ico %}'
)


def render(expr: str) -> str:
    return TEMPLATES.from_string(IMPORTS + "{{ " + expr + " }}").render().strip()


def paths(svg: str) -> str:
    """The drawing of an icon, without the wrapper element and its class."""
    return svg[svg.index(">", svg.index("<svg")) + 1 : svg.index("</svg>")]


FALLBACK = paths(render("icon('no-such-icon')"))


class IconSetTests(unittest.TestCase):
    def test_every_menu_item_has_a_drawn_icon(self) -> None:
        specs = [value for value in vars(panel_nav).values() if isinstance(value, panel_nav._ItemSpec)]
        for group in vars(panel_nav).values():
            if isinstance(group, (list, tuple)):
                specs += [item for item in group if isinstance(item, panel_nav._ItemSpec)]
        self.assertTrue(specs)
        for spec in specs:
            with self.subTest(item=spec.key):
                self.assertNotEqual(paths(render(f"icon('{spec.icon}')")), FALLBACK)

    def test_one_concept_has_one_drawing_across_macros(self) -> None:
        same = [
            ("page_ico('overview')", "nav_icon('shield-check')"),
            ("page_ico('panel-settings')", "nav_icon('sliders')"),
            ("page_ico('settings')", "nav_icon('sliders')"),
            ("card_title('orders', 'x')", "nav_icon('cart')"),
            ("card_title('nodes', 'x')", "nav_icon('nodes')"),
            ("action_center_icon('tickets')", "nav_icon('ticket')"),
            ("action_center_icon('pending')", "nav_icon('card')"),
            ("page_ico('payments')", "nav_icon('card')"),
            ("page_ico('activity')", "nav_icon('pulse')"),
            ("ico('users')", "page_ico('users')"),
        ]
        for left, right in same:
            with self.subTest(left=left):
                self.assertEqual(paths(render(left)), paths(render(right)))

    def test_names_that_used_to_fall_back_now_draw_their_icon(self) -> None:
        for name in ("activity", "receipt", "user-edit", "shop_broadcast", "reseller-color"):
            with self.subTest(name=name):
                self.assertNotEqual(paths(render(f"page_ico('{name}')")), FALLBACK)

    def test_the_warning_shield_of_risky_permissions_differs_from_the_menu_shield(self) -> None:
        self.assertNotEqual(paths(render("ico('shield')")), paths(render("nav_icon('shield')")))

    def test_icons_are_hidden_from_screen_readers_and_keep_their_class(self) -> None:
        svg = render("nav_icon('home')")
        self.assertIn('class="nav-ico"', svg)
        self.assertIn('aria-hidden="true"', svg)


if __name__ == "__main__":
    unittest.main()
