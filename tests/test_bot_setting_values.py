"""Number and choice settings typed in the bots: every number field is bounded, hostile input is refused.

The owner bot used to accept any ``float()`` for number fields without bounds, so «nan», «inf»,
a negative referral bonus or a negative Stars rate could be stored; the readers then crashed
(``int(float("nan"))``) or paid a negative amount.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


from app.bot.handlers import admin_settings, reseller_settings
from app.services.bot_setting_values import (
    NUMBER_BOUNDS,
    SELECT_OPTIONS,
    next_select_value,
    parse_number_setting,
    select_label,
)
from app.services.service_import_config import UNLIMITED_VALUE

HOSTILE_NUMBERS = ("nan", "NaN", "inf", "-inf", "1e9", "-1", "1.5", "", "   ", "true", "0x10", "١٢abc")


class BoundsCoverageTests(unittest.TestCase):
    def test_every_bot_number_field_has_bounds(self) -> None:
        for fields in (admin_settings.FIELDS, reseller_settings.FIELDS):
            for key, (_key, _label, kind) in fields.items():
                if kind == "number":
                    with self.subTest(key=key):
                        self.assertIn(key, NUMBER_BOUNDS)

    def test_every_bot_select_field_has_choices(self) -> None:
        for fields in (admin_settings.FIELDS, reseller_settings.FIELDS):
            for key, (_key, _label, kind) in fields.items():
                if kind == "select":
                    with self.subTest(key=key):
                        self.assertIn(key, SELECT_OPTIONS)

    def test_bounds_are_ordered_and_never_negative(self) -> None:
        for key, (low, high) in NUMBER_BOUNDS.items():
            with self.subTest(key=key):
                self.assertGreaterEqual(low, 0)
                self.assertLessEqual(low, high)


class WheelSettingsInBotTests(unittest.TestCase):
    """The bots edit the same wheel settings as the web panel, with the same limits."""

    def test_both_bots_offer_every_web_wheel_setting(self) -> None:
        from app.services.lucky_wheel_settings import BOT_FIELDS

        keys = {key for key, _label, _kind in BOT_FIELDS}
        self.assertIn("lucky_wheel_interval_days", keys)
        for fields in (admin_settings.FIELDS, reseller_settings.FIELDS):
            self.assertLessEqual(keys, set(fields))

    def test_wheel_numbers_use_the_panels_limits(self) -> None:
        self.assertEqual(parse_number_setting("lucky_wheel_interval_days", "۷"), "7")
        self.assertEqual(parse_number_setting("lucky_wheel_interval_days", "365"), "365")
        for raw in (*HOSTILE_NUMBERS, "366"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_number_setting("lucky_wheel_interval_days", raw)
        with self.assertRaises(ValueError):
            parse_number_setting("lucky_wheel_cooldown_seconds", "86401")


class WheelSettingsScreenTests(unittest.IsolatedAsyncioTestCase):
    """The club settings button opens the wheel group in the editor of the bot that owns the scope."""

    async def test_the_button_opens_the_wheel_fields_for_the_owner_and_for_a_shop(self) -> None:
        from app.bot.handlers.loyalty import wheel_settings_callback

        owner = wheel_settings_callback(None).split(":")
        shop = wheel_settings_callback(5).split(":")
        self.assertEqual(owner[:3], ["adm", "st", "sub"])
        self.assertEqual(shop[:3], ["res", "st", "sub"])

        callback = SimpleNamespace(message=AsyncMock(), answer=AsyncMock(), data=wheel_settings_callback(None))
        with patch.object(admin_settings, "get_all_settings", AsyncMock(return_value={})):
            await admin_settings._render_sub(callback, None, owner[3], owner[4])
        buttons = [b.callback_data for row in callback.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertTrue(any("lucky_wheel_interval_days" in b for b in buttons), buttons)

        callback = SimpleNamespace(message=AsyncMock(), answer=AsyncMock(), data=wheel_settings_callback(5))
        with (
            patch.object(reseller_settings, "get_setting", AsyncMock(return_value="0")),
            patch.object(reseller_settings, "safe_edit_text", AsyncMock()) as edit,
        ):
            await reseller_settings._render_sub(callback, None, shop[3], shop[4], 5)
        buttons = [b.callback_data for row in edit.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertTrue(any("lucky_wheel_interval_days" in b for b in buttons), buttons)


class ParseNumberTests(unittest.TestCase):
    def test_previously_unbounded_money_fields_refuse_hostile_input(self) -> None:
        for key in ("referral_bonus", "stars_toman_per_star", "custom_plan_price_per_gb", "custom_plan_min_gb"):
            for raw in HOSTILE_NUMBERS:
                with self.subTest(key=key, raw=raw), self.assertRaises(ValueError):
                    parse_number_setting(key, raw)

    def test_valid_values_are_stored_as_plain_integers(self) -> None:
        self.assertEqual(parse_number_setting("referral_bonus", "۵۰٬۰۰۰"), "50000")
        self.assertEqual(parse_number_setting("referral_bonus", "0"), "0")
        self.assertEqual(parse_number_setting("stars_toman_per_star", "1,200"), "1200")

    def test_stars_rate_cannot_be_zero(self) -> None:
        with self.assertRaises(ValueError):
            parse_number_setting("stars_toman_per_star", "0")

    def test_unknown_number_key_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            parse_number_setting("not_a_setting", "5")

    def test_unlimited_word_is_accepted_only_where_allowed(self) -> None:
        self.assertEqual(parse_number_setting("service_import_max_per_user", "نامحدود"), UNLIMITED_VALUE)
        with self.assertRaises(ValueError):
            parse_number_setting("referral_bonus", "نامحدود")

    def test_refusal_carries_the_range_hint(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_number_setting("admin_daily_report_hour", "24")
        self.assertIn("23", str(caught.exception))


class SelectTests(unittest.TestCase):
    def test_cycle_and_label(self) -> None:
        self.assertEqual(next_select_value("service_import_mode", "instant"), "approval")
        self.assertEqual(next_select_value("service_import_mode", "approval"), "instant")
        self.assertEqual(next_select_value("service_import_mode", "<b>junk</b>"), "approval")
        self.assertEqual(select_label("service_import_mode", "approval"), "با تأیید ادمین")
        self.assertEqual(select_label("service_import_mode", "junk"), "فوری")


class OwnerSaveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        actor = patch.object(admin_settings, "_ensure_settings_actor", AsyncMock(return_value=True))
        actor.start()
        self.addCleanup(actor.stop)

    async def test_nan_referral_bonus_is_not_saved(self) -> None:
        state = AsyncMock(get_data=AsyncMock(return_value={"edit_key": "referral_bonus"}))
        message = MagicMock(text="nan", answer=AsyncMock(), delete=AsyncMock())
        with (
            patch.object(admin_settings, "_is_admin", return_value=True),
            patch.object(admin_settings, "set_setting", AsyncMock()) as saved,
            patch.object(admin_settings, "_finish_settings_text_edit", AsyncMock()),
        ):
            await admin_settings.settings_edit_save(message, state, MagicMock(), SimpleNamespace(id=1))
        saved.assert_not_awaited()
        message.answer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
