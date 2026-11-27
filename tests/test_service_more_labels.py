"""«⚙️ بیشتر» of a service: readable labels, each action editable in the menu editor with its own color."""

from __future__ import annotations

import unittest

from app.bot.nav_inline import service_more_keyboard
from app.bot.screen_registry import ScreenContext, screen_buttons
from app.services.button_styles import setting_key
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

    def test_menu_editor_lists_the_link_change_button_with_its_text_and_color(self) -> None:
        buttons = screen_buttons("c.service_more", ScreenContext(ui=default_ui()))
        link = [button for button in buttons if button.label_key == "btn_svc_link_reset"]
        self.assertEqual(len(link), 1)
        self.assertTrue(link[0].is_editable)
        self.assertEqual(link[0].style_id, "svc_link_reset")

    def test_freeze_link_and_transfer_colors_do_not_follow_automation(self) -> None:
        ui = {setting_key("svc_auto"): "danger", setting_key("svc_freeze"): "success"}
        for freeze, transfer in (("pause", "start"), ("resume", "cancel")):
            markup = service_more_keyboard(1, ui, freeze=freeze, transfer=transfer, link_reset=True)
            styles = {
                button.callback_data.split(":")[1]: button.style
                for row in markup.inline_keyboard for button in row
            }
            self.assertEqual(styles["auto"], "danger")
            self.assertEqual(styles["frz" if freeze == "pause" else "frzres"], "success")
            self.assertNotEqual(styles["lnk"], "danger")
            self.assertNotEqual(styles["trn" if transfer == "start" else "trnx"], "danger")

    def test_each_action_has_its_own_color_setting_in_the_editor(self) -> None:
        buttons = screen_buttons("c.service_more", ScreenContext(ui=default_ui()))
        style_of = {button.label_key: button.style_id for button in buttons}
        self.assertEqual(style_of["btn_svc_auto"], "svc_auto")
        self.assertEqual(style_of["btn_svc_freeze"], "svc_freeze")
        self.assertEqual(style_of["btn_svc_transfer"], "svc_transfer")


if __name__ == "__main__":
    unittest.main()
