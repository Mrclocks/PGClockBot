"""Backup schedule settings in the owner bot, reached from the backup panel.

Only the schedule, the retention and the Telegram on/off switch are offered. The recipients, the
backup bot token and the .env switches stay on the web, so a chat cannot redirect backups or
secrets. A retention change applies at the next backup and never deletes files from a tap.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


from app.bot.backup_entry import BACKUP_PANEL_CALLBACK, BACKUP_SETTINGS_CALLBACK
from app.bot.handlers import admin_backup, admin_settings, reseller_settings
from app.services import bot_feedback_texts as feedback
from app.services.backup_settings import KEEP_CHOICES
from app.services.bot_setting_values import SELECT_OPTIONS, parse_number_setting

OFFERED = (
    "backup_schedule_enabled",
    "backup_schedule_hour",
    "backup_keep_count",
    "backup_telegram_enabled",
)
WEB_ONLY = (
    "backup_include_env_scheduled",
    "backup_telegram_allow_env",
    "backup_telegram_custom_bot",
    "backup_telegram_bot_token",
    "backup_telegram_chat_ids",
)


class FieldTests(unittest.TestCase):
    def test_owner_bot_offers_only_the_safe_backup_settings(self) -> None:
        for key in OFFERED:
            self.assertIn(key, admin_settings.FIELDS)
        for key in WEB_ONLY:
            self.assertNotIn(key, admin_settings.FIELDS)

    def test_shop_bot_has_no_backup_settings(self) -> None:
        for key in (*OFFERED, *WEB_ONLY):
            self.assertNotIn(key, reseller_settings.FIELDS)

    def test_retention_choices_match_the_web(self) -> None:
        self.assertEqual([value for value, _label in SELECT_OPTIONS["backup_keep_count"]], [str(c) for c in KEEP_CHOICES])

    def test_hour_is_bounded(self) -> None:
        self.assertEqual(parse_number_setting("backup_schedule_hour", "۲۳"), "23")
        for raw in ("24", "-1", "nan", "3.5"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_number_setting("backup_schedule_hour", raw)

    def test_section_is_not_on_the_settings_hub_and_returns_to_the_backup_panel(self) -> None:
        self.assertNotIn("backup", admin_settings.HUB_ORDER)
        self.assertEqual(admin_settings.SECTIONS["backup"]["back"], BACKUP_PANEL_CALLBACK)


class PanelTests(unittest.TestCase):
    def test_backup_panel_opens_the_settings(self) -> None:
        markup = admin_backup.backup_hub_markup([], {})
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn(BACKUP_SETTINGS_CALLBACK, callbacks)


class SettingsActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        actor = patch.object(admin_settings, "_ensure_settings_actor", AsyncMock(return_value=True))
        actor.start()
        self.addCleanup(actor.stop)

    async def test_section_back_button_returns_to_the_backup_panel(self) -> None:
        callback = MagicMock(message=MagicMock(edit_text=AsyncMock()))
        await admin_settings._render_section(callback, MagicMock(), "backup")
        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(callbacks[-1], BACKUP_PANEL_CALLBACK)
        self.assertIn("adm:st:sub:backup:schedule", callbacks)

    async def test_retention_tap_cycles_without_deleting_backups(self) -> None:
        callback = MagicMock(data="adm:st:sel:backup_keep_count", answer=AsyncMock(), message=MagicMock())
        with (
            patch.object(admin_settings, "_is_admin", return_value=True),
            patch.object(admin_settings, "get_setting", AsyncMock(return_value="15")),
            patch.object(admin_settings, "set_setting", AsyncMock()) as saved,
            patch.object(admin_settings, "_rerender_after_key", AsyncMock()),
            patch("app.services.backup_settings.prune_backups") as pruned,
        ):
            await admin_settings.settings_select_cycle(callback, MagicMock(), SimpleNamespace(id=1))
        self.assertEqual(saved.await_args.args[1:], ("backup_keep_count", "5"))
        pruned.assert_not_called()

    async def test_non_admin_cannot_change_backup_settings(self) -> None:
        callback = MagicMock(data="adm:st:tog:backup_schedule_enabled", answer=AsyncMock(), message=MagicMock())
        with (
            patch.object(admin_settings, "_is_admin", return_value=False),
            patch.object(admin_settings, "set_setting", AsyncMock()) as saved,
        ):
            await admin_settings.settings_toggle(callback, MagicMock(), SimpleNamespace(id=1))
        saved.assert_not_awaited()
        callback.answer.assert_awaited_once_with(feedback.NOT_ADMIN, show_alert=True)

    async def test_forged_callback_for_a_web_only_key_is_refused(self) -> None:
        for data in ("adm:st:tog:backup_telegram_allow_env", "adm:st:tog:backup_include_env_scheduled"):
            callback = MagicMock(data=data, answer=AsyncMock(), message=MagicMock())
            with (
                self.subTest(data=data),
                patch.object(admin_settings, "_is_admin", return_value=True),
                patch.object(admin_settings, "set_setting", AsyncMock()) as saved,
            ):
                await admin_settings.settings_toggle(callback, MagicMock(), SimpleNamespace(id=1))
                saved.assert_not_awaited()
        for key in ("backup_telegram_chat_ids", "backup_telegram_bot_token"):
            callback = MagicMock(data=f"adm:st:edit:{key}", answer=AsyncMock(), message=MagicMock())
            with (
                self.subTest(key=key),
                patch.object(admin_settings, "_is_admin", return_value=True),
                patch("app.bot.nav_input.ask_text", AsyncMock()) as asked,
            ):
                await admin_settings.settings_edit_ask(callback, AsyncMock(), MagicMock(), SimpleNamespace(id=1))
                asked.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
