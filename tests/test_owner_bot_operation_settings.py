"""Owner bot settings for customer usage alerts, action center, panel link health, sales state and more.

Each is the same setting the web edits, with the same kind, and number fields refuse values the
consumers would not accept.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


from app.bot.handlers import admin_settings
from app.services import bot_feedback_texts as feedback
from app.services.users import SETTING_GROUPS

NEW_KEYS = (
    "user_alert_low_enabled",
    "user_alert_low_traffic_pct",
    "user_alert_low_time_pct",
    "user_alert_time_text",
    "user_alert_traffic_text",
    "first_use_nudge_enabled",
    "action_center_expire_days",
    "one_tap_renew_enabled",
    "capacity_warn_pct",
    "funnel_tracking_enabled",
    "pg_link_watchdog_enabled",
    "pg_link_alerts_enabled",
    "shop_maintenance_enabled",
    "shop_maintenance_text",
    "receipt_auto_match_enabled",
    "receipt_match_window_minutes",
    "trial_min_account_minutes",
    "screen_editor_enabled",
)
NUMBER_LIMITS = {
    "user_alert_low_traffic_pct": (1, 100_000),
    "user_alert_low_time_pct": (1, 3650),
    "action_center_expire_days": (1, 365),
    "capacity_warn_pct": (1, 100),
    "receipt_match_window_minutes": (1, 10080),
    "trial_min_account_minutes": (0, 10080),
}


# The web edits a threshold as one number with a unit select; the bot offers the number field.
WEB_TO_BOT_KIND = {"threshold": "number"}
ALERT_UNIT_KEYS = ("user_alert_low_traffic_unit", "user_alert_low_time_unit")


def _web_kind(key: str) -> str:
    kind = next(field[2] for fields in SETTING_GROUPS.values() for field in fields if field[0] == key)
    return WEB_TO_BOT_KIND.get(kind, kind)


class FieldTests(unittest.TestCase):
    def test_every_new_setting_is_a_bot_field_with_the_web_kind(self) -> None:
        for key in NEW_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, admin_settings.FIELDS)
                self.assertEqual(admin_settings.FIELDS[key][2], _web_kind(key))

    def test_every_number_field_has_bounds(self) -> None:
        numbers = {key for key in NEW_KEYS if admin_settings.FIELDS[key][2] == "number"}
        self.assertEqual(numbers, set(NUMBER_LIMITS))
        for key, bounds in NUMBER_LIMITS.items():
            self.assertEqual(admin_settings.NUMBER_INT_RANGES[key], bounds, key)


    def test_alert_threshold_units_are_choices_with_the_web_units(self) -> None:
        web_units = {
            field[4][0]: [value for value, _label in field[4][1]]
            for fields in SETTING_GROUPS.values()
            for field in fields
            if field[2] == "threshold"
        }
        for key in ALERT_UNIT_KEYS:
            with self.subTest(key=key):
                self.assertEqual(admin_settings.FIELDS[key][2], "select")
                options = [value for value, _label in admin_settings.SELECT_CYCLE_OPTIONS[key]]
                self.assertEqual(options, web_units[key])


class NumberParsingTests(unittest.TestCase):
    def test_bounds_are_inclusive(self) -> None:
        for key, (low, high) in NUMBER_LIMITS.items():
            with self.subTest(key=key):
                self.assertEqual(admin_settings._parse_number_setting(key, str(low)), str(low))
                self.assertEqual(admin_settings._parse_number_setting(key, str(high)), str(high))

    def test_out_of_range_and_malformed_values_are_refused(self) -> None:
        for key, (low, high) in NUMBER_LIMITS.items():
            for raw in (str(low - 1), str(high + 1), "-5", "1.5", "nan", "inf", "1e3", "", "true", "۱۰۰۰۰۰۰۰"):
                with self.subTest(key=key, raw=raw), self.assertRaises(ValueError):
                    admin_settings._parse_number_setting(key, raw)

    def test_persian_digits_are_accepted(self) -> None:
        self.assertEqual(admin_settings._parse_number_setting("capacity_warn_pct", "۸۰"), "80")


class NotifyScreenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        actor = patch.object(admin_settings, "_ensure_settings_actor", AsyncMock(return_value=True))
        actor.start()
        self.addCleanup(actor.stop)

    async def test_notify_screen_opens_every_sub_screen(self) -> None:
        callback = MagicMock(message=MagicMock(edit_text=AsyncMock()))
        with patch.object(admin_settings, "get_all_settings", AsyncMock(return_value={})):
            await admin_settings._render_notify(callback, MagicMock())
        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        callbacks = {b.callback_data for row in markup.inline_keyboard for b in row}
        for sub_id in ("daily", "usage_alerts", "operations", "pg_link"):
            self.assertIn(f"adm:st:sub:notify:{sub_id}", callbacks)

    async def test_toggle_inside_a_notify_sub_screen_stays_on_that_screen(self) -> None:
        callback = MagicMock(data="adm:st:tog:user_alert_low_enabled", answer=AsyncMock(), message=MagicMock())
        with (
            patch.object(admin_settings, "_is_admin", return_value=True),
            patch.object(admin_settings, "get_setting", AsyncMock(return_value="0")),
            patch.object(admin_settings, "set_setting", AsyncMock()) as saved,
            patch.object(admin_settings, "_render_sub", AsyncMock()) as sub,
            patch.object(admin_settings, "_render_notify", AsyncMock()) as notify_home,
        ):
            await admin_settings.settings_toggle(callback, MagicMock(), SimpleNamespace(id=1))
        saved.assert_awaited_once()
        self.assertEqual(saved.await_args.args[1:], ("user_alert_low_enabled", "1"))
        # Platform value: no shop scope is passed.
        self.assertNotIn("reseller_id", saved.await_args.kwargs)
        sub.assert_awaited_once()
        self.assertEqual(sub.await_args.args[2:], ("notify", "usage_alerts"))
        notify_home.assert_not_awaited()

    async def test_non_admin_cannot_flip_a_platform_switch(self) -> None:
        callback = MagicMock(data="adm:st:tog:pg_link_watchdog_enabled", answer=AsyncMock(), message=MagicMock())
        with (
            patch.object(admin_settings, "_is_admin", return_value=False),
            patch.object(admin_settings, "set_setting", AsyncMock()) as saved,
        ):
            await admin_settings.settings_toggle(callback, MagicMock(), SimpleNamespace(id=1))
        saved.assert_not_awaited()
        callback.answer.assert_awaited_once_with(feedback.NOT_ADMIN, show_alert=True)


class NumberSaveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        actor = patch.object(admin_settings, "_ensure_settings_actor", AsyncMock(return_value=True))
        actor.start()
        self.addCleanup(actor.stop)

    async def save(self, key: str, text: str) -> tuple[AsyncMock, MagicMock]:
        state = AsyncMock(get_data=AsyncMock(return_value={"edit_key": key}))
        message = MagicMock(text=text, answer=AsyncMock(), delete=AsyncMock())
        with (
            patch.object(admin_settings, "_is_admin", return_value=True),
            patch.object(admin_settings, "set_setting", AsyncMock()) as saved,
            patch.object(admin_settings, "_finish_settings_text_edit", AsyncMock()),
        ):
            await admin_settings.settings_edit_save(message, state, MagicMock(), SimpleNamespace(id=1))
        return saved, message

    async def test_valid_number_is_saved(self) -> None:
        saved, _ = await self.save("receipt_match_window_minutes", "90")
        self.assertEqual(saved.await_args.args[1:], ("receipt_match_window_minutes", "90"))

    async def test_out_of_range_number_is_refused_with_a_hint(self) -> None:
        saved, message = await self.save("capacity_warn_pct", "250")
        saved.assert_not_awaited()
        self.assertIn("1", message.answer.await_args.args[0])
        self.assertIn("100", message.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
