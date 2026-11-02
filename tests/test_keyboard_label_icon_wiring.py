"""Inline keyboard label display and premium icon wiring."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from aiogram.types import MessageEntity

from app.bot.keyboards import _ikb, back_button, custom_days_keyboard, plans_keyboard
from app.services.rich_text import pack_rich_text


def _packed(label: str, emoji_id: str) -> str:
    entity = MessageEntity(type="custom_emoji", offset=0, length=1, custom_emoji_id=emoji_id)
    return pack_rich_text(label, [entity])


class KeyboardLabelIconWiringTests(unittest.TestCase):
    def test_ikb_action_resolves_icon_from_packed_setting(self) -> None:
        ui = {"btn_loyalty": _packed("⭐ باشگاه", "77")}
        button = _ikb("باشگاه", callback_data="loy:home", ui=ui, action="loyalty")
        self.assertEqual(button.icon_custom_emoji_id, "77")

    def test_back_button_uses_display_text_and_icon(self) -> None:
        ui = {"btn_back": _packed("⬅ بازگشت", "99")}
        button = back_button(ui, "menu:home")
        self.assertEqual(button.text.strip(), "بازگشت")
        self.assertEqual(button.icon_custom_emoji_id, "99")
        self.assertEqual(button.callback_data, "menu:home")

    def test_custom_days_has_single_control_per_callback(self) -> None:
        markup = custom_days_keyboard(30, {})
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(callbacks.count("shop:custom:confirm"), 1)

    def test_plan_button_inherits_category_icon(self) -> None:
        plan = SimpleNamespace(
            id=1, name="A", price=1000, category_id=5, button_style=None, is_trial=False
        )
        category = SimpleNamespace(
            id=5, name="C", button_style=None, icon_custom_emoji_id="555"
        )
        markup = plans_keyboard([plan], {}, kind="fixed", categories={5: category})
        self.assertEqual(markup.inline_keyboard[0][0].icon_custom_emoji_id, "555")


if __name__ == "__main__":
    unittest.main()
