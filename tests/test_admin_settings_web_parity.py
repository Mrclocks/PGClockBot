"""Owner bot settings that used to be web-only: same keys, same bounds and choices, same side effects."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.bot.handlers import admin_settings
from app.services.users import SETTING_GROUPS

NEW_KEYS = {
    "auto_approve_after_minutes": "number",
    "auto_approve_after_max_amount": "number",
    "auto_approve_after_returning_only": "toggle",
    "service_import_enabled": "toggle",
    "service_import_mode": "select",
    "service_import_max_per_user": "number",
    "admin_daily_report_enabled": "toggle",
    "subordinate_report_enabled": "toggle",
    "admin_daily_report_hour": "number",
    "miniapp_shops_enabled": "toggle",
    "miniapp_notice": "text",
}
# Kept on the web on purpose: they describe the one panel or need the web's editor.
WEB_ONLY_KEYS = ("service_import_paths", "service_import_admins", "admin_daily_report_template", "miniapp_accent_color")


def _web_field(key: str) -> tuple:
    return next(f for fields in SETTING_GROUPS.values() for f in fields if f[0] == key)


class FieldTests(unittest.TestCase):
    def test_each_web_setting_has_a_bot_field_of_the_same_kind(self) -> None:
        for key, kind in NEW_KEYS.items():
            with self.subTest(key=key):
                self.assertEqual(admin_settings.FIELDS[key][2], kind)
        for key in WEB_ONLY_KEYS:
            self.assertNotIn(key, admin_settings.FIELDS)

    def test_the_import_mode_offers_the_web_choices(self) -> None:
        web_options = dict(_web_field("service_import_mode")[4])
        self.assertEqual(dict(admin_settings.SELECT_CYCLE_OPTIONS["service_import_mode"]), web_options)

    def test_the_new_screens_stay_within_eight_fields(self) -> None:
        new_subs = {"auto_after", "import", "daily", "miniapp"}
        found = set()
        for sec in admin_settings.SECTIONS.values():
            for sub_id, _title, payload, *_ in sec.get("subs") or []:
                if sub_id in new_subs:
                    found.add(sub_id)
                    self.assertLessEqual(len(payload), 8, sub_id)
        self.assertEqual(found, new_subs)


def _message(text: str) -> MagicMock:
    return MagicMock(text=text, answer=AsyncMock(), delete=AsyncMock())


class SaveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        actor = patch.object(admin_settings, "_ensure_settings_actor", AsyncMock(return_value=True))
        actor.start()
        self.addCleanup(actor.stop)

    async def save(self, key: str, text: str):
        state = AsyncMock(get_data=AsyncMock(return_value={"edit_key": key}))
        message = _message(text)
        with (
            patch.object(admin_settings, "_is_admin", return_value=True),
            patch.object(admin_settings, "set_setting", AsyncMock()) as saved,
            patch.object(admin_settings, "_finish_settings_text_edit", AsyncMock()),
        ):
            await admin_settings.settings_edit_save(message, state, MagicMock(), SimpleNamespace(id=1))
        return saved, message

    async def test_numbers_outside_the_web_bounds_are_refused(self) -> None:
        for key, bad in (
            ("admin_daily_report_hour", "24"),
            ("admin_daily_report_hour", "-1"),
            ("auto_approve_after_minutes", "1441"),
            ("auto_approve_after_max_amount", "2000000001"),
            ("service_import_max_per_user", "0"),
            ("service_import_max_per_user", "21"),
            ("service_import_max_per_user", "NaN"),
            ("auto_approve_after_minutes", "1.5"),
        ):
            with self.subTest(key=key, bad=bad):
                saved, message = await self.save(key, bad)
                saved.assert_not_awaited()
                message.answer.assert_awaited_once()

    async def test_numbers_inside_the_bounds_are_saved(self) -> None:
        for key, good in (("admin_daily_report_hour", "23"), ("service_import_max_per_user", "20"), ("auto_approve_after_minutes", "۳۰")):
            with self.subTest(key=key):
                saved, _ = await self.save(key, good)
                self.assertEqual(saved.await_args.args[1], key)
                self.assertTrue(saved.await_args.args[2].isdigit())

    async def test_the_mini_app_notice_is_one_plain_line_within_the_limit(self) -> None:
        saved, _ = await self.save("miniapp_notice", "  تخفیف\nامروز  ")
        self.assertEqual(saved.await_args.args[1:], ("miniapp_notice", "تخفیف امروز"))
        saved, message = await self.save("miniapp_notice", "x" * 201)
        saved.assert_not_awaited()
        message.answer.assert_awaited_once()


class MiniAppSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_switch_syncs_shop_menus_only_when_it_changes(self) -> None:
        for changed in (True, False):
            with (
                patch.object(admin_settings, "get_setting", AsyncMock(return_value="0")),
                patch.object(admin_settings, "set_platform_shop_miniapps", AsyncMock(return_value=changed)) as switch,
                patch.object(admin_settings, "schedule_shop_menu_sync") as sync,
                patch.object(admin_settings, "set_setting", AsyncMock()) as plain_set,
            ):
                await admin_settings._flip_setting(MagicMock(), "miniapp_shops_enabled")
            switch.assert_awaited_once()
            self.assertTrue(switch.await_args.kwargs["enabled"])
            self.assertEqual(sync.called, changed)
            plain_set.assert_not_awaited()

    async def test_other_switches_flip_plainly(self) -> None:
        with (
            patch.object(admin_settings, "get_setting", AsyncMock(return_value="1")),
            patch.object(admin_settings, "set_setting", AsyncMock()) as plain_set,
            patch.object(admin_settings, "schedule_shop_menu_sync") as sync,
        ):
            await admin_settings._flip_setting(MagicMock(), "service_import_enabled")
        self.assertEqual(plain_set.await_args.args[1:], ("service_import_enabled", "0"))
        sync.assert_not_called()


class NotifyScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_notify_screen_links_the_daily_report(self) -> None:
        callback = MagicMock(message=MagicMock(edit_text=AsyncMock()))
        with patch.object(admin_settings, "get_all_settings", AsyncMock(return_value={})):
            await admin_settings._render_notify(callback, MagicMock())
        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        found = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("adm:st:sub:notify:daily", found)
