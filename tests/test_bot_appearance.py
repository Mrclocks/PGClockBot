"""Tests for Telegram bot appearance helpers."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.services.bot_appearance import (
    DESCRIPTION_MAX,
    NAME_MAX,
    SHORT_DESCRIPTION_MAX,
    appearance_to_form_dict,
    clip,
    validate_appearance_form,
    BotAppearance,
)

ROOT = Path(__file__).resolve().parents[1]


class ClipTests(unittest.TestCase):
    def test_clip_trims_and_limits(self):
        self.assertEqual(clip("  abc  ", 2), "ab")
        self.assertEqual(clip(None, 5), "")


class ValidateTests(unittest.TestCase):
    def test_ok(self):
        self.assertIsNone(
            validate_appearance_form(
                name="Clock",
                description="about",
                short_description="hi",
            )
        )

    def test_name_too_long(self):
        err = validate_appearance_form(
            name="x" * (NAME_MAX + 1),
            description="",
            short_description="",
        )
        self.assertIn("نام", err or "")

    def test_description_too_long(self):
        err = validate_appearance_form(
            name="n",
            description="d" * (DESCRIPTION_MAX + 1),
            short_description="",
        )
        self.assertIn("وسط", err or "")

    def test_short_too_long(self):
        err = validate_appearance_form(
            name="n",
            description="",
            short_description="s" * (SHORT_DESCRIPTION_MAX + 1),
        )
        self.assertIn("کپشن", err or "")


class FormDictTests(unittest.TestCase):
    def test_maps_fields(self):
        app = BotAppearance(
            name="A",
            username="bot",
            description="D",
            short_description="S",
        )
        d = appearance_to_form_dict(app, local_photo="uploads/x.jpg")
        self.assertEqual(d["bot_tg_name"], "A")
        self.assertEqual(d["bot_username"], "bot")
        self.assertEqual(d["bot_tg_photo"], "uploads/x.jpg")
        self.assertNotIn("bot_cmd_start", d)
        self.assertNotIn("bot_cmd_help", d)


class CommandsUiRemovedTests(unittest.TestCase):
    def test_settings_form_has_no_command_fields(self):
        ap = (ROOT / "app/web/templates/_settings_appearance.html").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("دستورهای منوی ربات", ap)
        self.assertNotIn("bot_cmd_start", ap)
        self.assertNotIn("bot_cmd_help", ap)
        self.assertNotIn("ap-cmd-start", ap)

    def test_service_no_longer_reads_or_sets_command_descriptions(self):
        src = (ROOT / "app/services/bot_appearance.py").read_text(encoding="utf-8")
        self.assertNotIn("get_my_commands", src)
        self.assertNotIn("set_my_commands", src)
        self.assertNotIn("form.get(\"bot_cmd_start\")", src)
        self.assertNotIn("form.get(\"bot_cmd_help\")", src)
        self.assertNotIn("DEFAULT_CMD_", src)
        self.assertIn("delete_my_commands", src)
        # Legacy keys cleared on save so old DB values disappear
        self.assertIn('"bot_cmd_start": ""', src)
        self.assertIn('"bot_cmd_help": ""', src)

    def test_defaults_drop_command_keys(self):
        from app.services.users import DEFAULT_SETTINGS

        self.assertNotIn("bot_cmd_start", DEFAULT_SETTINGS)
        self.assertNotIn("bot_cmd_help", DEFAULT_SETTINGS)


class TabsTests(unittest.TestCase):
    def test_appearance_in_admin_and_reseller_tabs(self):
        from app.services.resellers import RESELLER_SETTINGS_TABS
        from app.services.users import SETTINGS_TABS

        self.assertIn("appearance", {t[0] for t in SETTINGS_TABS})
        self.assertIn("appearance", {t[0] for t in RESELLER_SETTINGS_TABS})


if __name__ == "__main__":
    unittest.main()
