"""Backup pg_dump PATH + button premium icon_custom_emoji_id."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from aiogram.types import MessageEntity

from app.bot.keyboards import _ikb, _kb, _t
from app.services.rich_text import (
    button_display_text,
    button_icon_custom_emoji_id,
    button_icon_from_ui,
    is_button_label_key,
    merge_rich_settings_on_save,
    pack_rich_text,
    pack_setting_from_message,
    prepare_settings_values_for_web,
    rich_plain_text,
)


ROOT = Path(__file__).resolve().parents[1]


class SystemdPathBackupTests(unittest.TestCase):
    def test_service_path_includes_usr_bin(self):
        src = (ROOT / "pgclock.sh").read_text(encoding="utf-8")
        self.assertIn("/usr/bin", src)
        self.assertIn("ensure_pg_client_tools", src)
        # Old unit PATH was only venv — that hid pg_dump from the bot process.
        self.assertNotIn(
            "Environment=PATH=${SCRIPT_DIR}/.venv/bin\n",
            src,
        )
        self.assertIn(
            "Environment=PATH=${SCRIPT_DIR}/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            src,
        )

    def test_update_refreshes_pg_client_and_unit(self):
        src = (ROOT / "pgclock.sh").read_text(encoding="utf-8")
        # cmd_update must reinstall client tools + rewrite unit PATH.
        update = src.split("cmd_update()", 1)[1].split("cmd_", 1)[0]
        self.assertIn("ensure_pg_client_tools", update)
        self.assertIn("install_systemd", update)

    def test_which_falls_back_to_usr_bin(self):
        from app.services import backup_postgres

        with patch.object(backup_postgres.shutil, "which", return_value=None):
            with patch.object(backup_postgres.os, "access", return_value=True):
                with patch.object(Path, "is_file", return_value=True):
                    found = backup_postgres.find_tool("pg_dump")
        self.assertEqual(found, "/usr/bin/pg_dump")


class ButtonIconPremiumTests(unittest.TestCase):
    def test_button_label_keys(self):
        self.assertTrue(is_button_label_key("btn_shop"))
        self.assertFalse(is_button_label_key("btn_style_shop"))
        self.assertFalse(is_button_label_key("welcome_text"))

    def test_pack_and_display_strips_custom_emoji(self):
        body = "🛒 خرید سرویس"
        ent = [
            MessageEntity(
                type="custom_emoji", offset=0, length=2, custom_emoji_id="4242"
            )
        ]
        packed = pack_rich_text(body, ent)
        self.assertEqual(button_icon_custom_emoji_id(packed), "4242")
        self.assertEqual(button_display_text(packed), "خرید سرویس")
        self.assertEqual(rich_plain_text(packed), body)

    def test_kb_and_ikb_set_icon(self):
        body = "⭐ باشگاه"
        ent = [
            MessageEntity(
                type="custom_emoji", offset=0, length=2, custom_emoji_id="77"
            )
        ]
        ui = {"btn_loyalty": pack_rich_text(body, ent)}
        self.assertEqual(_t(ui, "btn_loyalty"), "باشگاه")
        kb = _kb(_t(ui, "btn_loyalty"), action="loyalty", ui=ui)
        self.assertEqual(kb.icon_custom_emoji_id, "77")
        self.assertEqual(kb.text, "باشگاه")
        ikb = _ikb(
            _t(ui, "btn_loyalty"),
            callback_data="loy:home",
            ui=ui,
            label_key="btn_loyalty",
        )
        self.assertEqual(ikb.icon_custom_emoji_id, "77")

    def test_pack_setting_from_message_buttons(self):
        class Msg:
            text = "x😀"
            entities = [
                MessageEntity(
                    type="custom_emoji", offset=1, length=2, custom_emoji_id="1"
                )
            ]

        packed = pack_setting_from_message("btn_shop", Msg())
        self.assertTrue(packed.startswith("\x1eRICH1:"))
        # Message bodies also pack when they are MESSAGE_RICH_KEYS.
        packed_msg = pack_setting_from_message("welcome_text", Msg())
        self.assertTrue(packed_msg.startswith("\x1eRICH1:"))
        plain = pack_setting_from_message("some_number_key", Msg())
        self.assertEqual(plain, "x😀")

    def test_web_merge_keeps_button_icon(self):
        body = "🛒 خرید"
        packed = pack_rich_text(
            body,
            [
                MessageEntity(
                    type="custom_emoji", offset=0, length=2, custom_emoji_id="9"
                )
            ],
        )
        existing = {"btn_shop": packed}
        web = prepare_settings_values_for_web(dict(existing))
        self.assertEqual(web["btn_shop"], body)
        payload = {"btn_shop": body}
        merge_rich_settings_on_save(existing, payload)
        self.assertEqual(payload["btn_shop"], packed)
        self.assertEqual(button_icon_from_ui(existing, label_key="btn_shop"), "9")


if __name__ == "__main__":
    unittest.main()
