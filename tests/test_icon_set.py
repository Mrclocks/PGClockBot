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

    def test_settings_are_drawn_as_a_gear_everywhere(self) -> None:
        gear = paths(render("icon('gear')"))
        self.assertNotEqual(gear, FALLBACK)
        self.assertNotEqual(gear, paths(render("icon('sliders')")))
        for name in ("settings", "panel-settings"):
            self.assertEqual(paths(render(f"icon('{name}')")), gear, name)
        specs = [group for group in vars(panel_nav).values() if isinstance(group, (list, tuple))]
        system = [item for group in specs for item in group
                  if isinstance(item, panel_nav._ItemSpec) and item.key == "system_settings"]
        self.assertEqual([item.icon for item in system], ["gear"])

    def test_icons_added_for_the_former_hand_drawn_ones_draw_something(self) -> None:
        for name in ("eye", "chev-down", "chev-up", "minus", "dots-v", "money", "monitor", "sun", "moon", "updown",
                     "logout", "check-circle", "plus-square", "github", "lock"):
            with self.subTest(icon=name):
                self.assertNotEqual(paths(render(f"icon('{name}')")), FALLBACK)

    def test_dot_menus_draw_filled_dots_under_any_stylesheet(self) -> None:
        # Row actions style their icon as a filled shape without a stroke; zero-length stroked dots vanished there.
        for name in ("dots", "dots-v"):
            svg = render(f"icon('{name}')")
            self.assertEqual(svg.count('fill="currentColor" stroke="none"'), 3, name)
            self.assertNotIn("h.01", svg)

    def test_resellers_have_their_own_icon_apart_from_users_and_referrals(self) -> None:
        reseller = paths(render("icon('resellers')"))
        self.assertNotEqual(reseller, FALLBACK)
        for other in ("users", "users-pair"):
            self.assertNotEqual(reseller, paths(render(f"icon('{other}')")), other)
        specs = [item for group in vars(panel_nav).values() if isinstance(group, (list, tuple))
                 for item in group if isinstance(item, panel_nav._ItemSpec)]
        reseller_items = [spec for spec in specs if spec.href == "/resellers"]
        self.assertTrue(reseller_items)
        for spec in reseller_items:
            self.assertEqual(paths(render(f"icon('{spec.icon}')")), reseller, spec.key)

    def test_the_guide_draws_the_users_icon_like_the_panel(self) -> None:
        from scripts.build_guide import icon_svg  # the guide builder is a script, imported only for this check

        self.assertEqual(paths(str(icon_svg("users"))), paths(render("icon('users')")))
        self.assertNotEqual(paths(render("icon('users')")), paths(render("icon('resellers')")))

    def test_an_icon_draws_as_an_outline_even_without_a_stylesheet(self) -> None:
        svg = render("icon('close')")
        for attribute in ('fill="none"', 'stroke="currentColor"', 'stroke-linecap="round"'):
            self.assertIn(attribute, svg)

    def test_one_concept_has_one_drawing_across_macros(self) -> None:
        same = [
            ("page_ico('overview')", "nav_icon('shield-check')"),
            ("page_ico('panel-settings')", "nav_icon('gear')"),
            ("page_ico('settings')", "nav_icon('gear')"),
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
