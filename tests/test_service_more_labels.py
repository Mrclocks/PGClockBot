"""«⚙️ بیشتر» of a service shows readable freeze/transfer labels and lists them for the menu editor."""

from __future__ import annotations

import unittest

from app.bot.nav_inline import service_more_keyboard
from app.bot.screen_registry import ScreenContext, screen_buttons
from tests.test_bot_ui_keyboard_snapshots import default_ui

_FREEZE_TRANSFER_KEYS = ("btn_svc_freeze", "btn_svc_unfreeze", "btn_svc_transfer", "btn_svc_transfer_cancel")


class ServiceMoreLabelTests(unittest.TestCase):
    def test_freeze_and_transfer_buttons_show_text_not_setting_keys(self) -> None:
        for freeze, transfer in (("pause", "start"), ("resume", "cancel")):
            markup = service_more_keyboard(1, {}, freeze=freeze, transfer=transfer)
            texts = [button.text for row in markup.inline_keyboard for button in row]
            self.assertFalse([text for text in texts if text.startswith("btn_")], texts)

    def test_menu_editor_lists_freeze_and_transfer_labels(self) -> None:
        buttons = screen_buttons("c.service_more", ScreenContext(ui=default_ui()))
        label_keys = {button.label_key for button in buttons}
        self.assertTrue({"btn_svc_freeze", "btn_svc_transfer"} <= label_keys)
        for button in buttons:
            if button.label_key in _FREEZE_TRANSFER_KEYS:
                self.assertTrue(button.is_editable)


if __name__ == "__main__":
    unittest.main()
