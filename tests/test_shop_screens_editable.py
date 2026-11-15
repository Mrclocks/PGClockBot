"""The shop's first step and its plan lists are screens of the menu editor."""

from __future__ import annotations

import unittest

from app.bot import keyboards as kb
from app.bot.edit_mode import EDIT_ROW_TEXT, editing_as
from app.bot.screen_registry import ScreenContext, screen_buttons
from app.services.screen_editor import EditorActor
from app.services.settings_button_labels import all_btn_field_keys
from app.services.users import DEFAULT_SETTINGS

OWNER = EditorActor(user_id=7, shop_id=None)
SHOP_OWNER = EditorActor(user_id=8, shop_id=900)
SHOP_ENTRIES = ("btn_shop_kind_trial", "btn_shop_kind_fixed", "btn_shop_kind_custom", "btn_wholesale")


def _texts(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


class EditRowTests(unittest.TestCase):
    def test_the_edit_row_appears_under_the_shop_screens_while_editing(self) -> None:
        builders = {
            "shop": lambda: kb.shop_kind_keyboard({}, fixed_on=True, trial_on=True),
            "plans": lambda: kb.plans_keyboard([], {}),
            "wholesale": lambda: kb.wholesale_plans_keyboard([], {}),
        }
        for name, build in builders.items():
            for actor in (OWNER, SHOP_OWNER):
                with self.subTest(screen=name, shop=actor.shop_id), editing_as(actor):
                    self.assertIn(EDIT_ROW_TEXT, _texts(build()))

    def test_customers_never_see_the_edit_row(self) -> None:
        self.assertNotIn(EDIT_ROW_TEXT, _texts(kb.shop_kind_keyboard({}, fixed_on=True, trial_on=True)))
        self.assertNotIn(EDIT_ROW_TEXT, _texts(kb.plans_keyboard([], {})))


class ShopEntryTests(unittest.TestCase):
    def test_every_shop_entry_is_a_real_setting_the_editor_can_save(self) -> None:
        reported = {b.label_key for b in screen_buttons("c.shop", ScreenContext(ui={}))}
        labels = set(all_btn_field_keys(include_platform=False))  # a shop owner can edit them too
        for key in SHOP_ENTRIES:
            with self.subTest(key=key):
                self.assertIn(key, reported)
                self.assertIn(key, DEFAULT_SETTINGS)
                self.assertIn(key, labels)

    def test_a_renamed_entry_is_shown_with_its_new_text(self) -> None:
        markup = kb.shop_kind_keyboard({"btn_shop_kind_trial": "🎁 تست رایگان"}, trial_on=True)
        self.assertIn("🎁 تست رایگان", _texts(markup))

    def test_the_default_texts_are_unchanged(self) -> None:
        texts = _texts(kb.shop_kind_keyboard(dict(DEFAULT_SETTINGS), fixed_on=True, trial_on=True, custom_on=True))
        self.assertEqual(texts[:3], ["تست", "ثابت", "دلخواه"])

    def test_the_custom_plan_color_is_editable_unless_its_own_setting_overrides_it(self) -> None:
        def custom_button(ui: dict) -> object:
            return next(b for row in kb.shop_kind_keyboard(ui, custom_on=True).inline_keyboard for b in row
                        if b.callback_data == "shop:kind:custom")

        self.assertEqual(custom_button({"btn_style_shop_kind_custom": "danger"}).style, "danger")
        overridden = {"btn_style_shop_kind_custom": "danger", "custom_plan_button_style": "success"}
        self.assertEqual(custom_button(overridden).style, "success")
        reported = {b.text: b.style_id for b in screen_buttons("c.shop", ScreenContext(ui={}))}
        self.assertEqual(reported["دلخواه"], "shop_kind_custom")


if __name__ == "__main__":
    unittest.main()
