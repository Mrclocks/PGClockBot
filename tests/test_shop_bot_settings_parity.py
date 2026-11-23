"""Shop bot settings: the shop-level settings of «/shop-settings», with number and choice fields.

Every save is written to the authenticated shop only. Number fields refuse values outside the
consumers' bounds (the shop bot used to store entry-approval numbers as typed), and a choice is
cycled on the shop's own copy.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


from app.bot.handlers import reseller_settings
from app.services import bot_feedback_texts as feedback
from app.services.users import SETTING_GROUPS

SHOP_ID = 11
NEW_SHOP_KEYS = (
    "user_alert_low_enabled",
    "user_alert_low_traffic_pct",
    "user_alert_low_time_pct",
    "user_alert_time_text",
    "user_alert_traffic_text",
    "first_use_nudge_enabled",
    "trial_min_account_minutes",
    "service_import_enabled",
    "service_import_mode",
    "service_import_max_per_user",
    "pg_username_prefix",
    "pg_username_suffix",
    "pg_username_pattern",
    "admin_daily_report_enabled",
    "subordinate_report_enabled",
    "admin_daily_report_hour",
    "btn_buy_continue",
    "shop_maintenance_enabled",
    "shop_maintenance_text",
    "receipt_auto_match_enabled",
    "receipt_match_window_minutes",
    "auto_approve_after_minutes",
    "auto_approve_after_max_amount",
    "auto_approve_after_returning_only",
    "stars_toman_per_star",
    "stars_title",
    "stars_description",
    "referral_bonus",
)
OWNER_ONLY_KEYS = (
    "action_center_expire_days",
    "one_tap_renew_enabled",
    "capacity_warn_pct",
    "funnel_tracking_enabled",
    "pg_link_watchdog_enabled",
    "pg_link_alerts_enabled",
    "service_import_paths",
    "service_import_admins",
    "screen_editor_enabled",
    "custom_service_name_enabled",
)


def _profile() -> SimpleNamespace:
    return SimpleNamespace(user_id=SHOP_ID)


def _web_kind(key: str) -> str:
    return next(field[2] for fields in SETTING_GROUPS.values() for field in fields if field[0] == key)


class FieldTests(unittest.TestCase):
    def test_shop_bot_offers_the_shop_level_settings_with_web_kinds(self) -> None:
        for key in NEW_SHOP_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, reseller_settings.FIELDS)
                kind = reseller_settings.FIELDS[key][2]
                web = _web_kind(key)
                # The web shows the import cap as a number with an «unlimited» button, and an alert
                # threshold as a number with a unit select; the bot edits the number.
                self.assertEqual(kind, "number" if web in {"number_unlimited", "threshold"} else web)

    def test_shop_bot_edits_the_alert_threshold_units_as_selects(self) -> None:
        for key in ("user_alert_low_traffic_unit", "user_alert_low_time_unit"):
            with self.subTest(key=key):
                self.assertEqual(reseller_settings.FIELDS[key][2], "select")

    def test_shop_bot_never_offers_platform_settings(self) -> None:
        for key in OWNER_ONLY_KEYS:
            self.assertNotIn(key, reseller_settings.FIELDS)


class ShopEditHarness(unittest.IsolatedAsyncioTestCase):
    def gate(self, profile=None, err=None):
        return patch.object(reseller_settings, "_gate", AsyncMock(return_value=(profile or _profile(), err)))

    async def save(self, key: str, text: str) -> tuple[AsyncMock, MagicMock]:
        state = AsyncMock(get_data=AsyncMock(return_value={"edit_key": key}))
        message = MagicMock(text=text, entities=None, answer=AsyncMock(), delete=AsyncMock())
        with (
            self.gate(),
            patch.object(reseller_settings, "set_setting", AsyncMock()) as saved,
            patch.object(reseller_settings, "_close_input_to_location", AsyncMock()),
        ):
            await reseller_settings.settings_edit_save(message, state, MagicMock(), SimpleNamespace(id=1))
        return saved, message


class NumberSaveTests(ShopEditHarness):
    async def test_number_in_range_is_saved_for_the_shop(self) -> None:
        saved, _ = await self.save("user_alert_low_traffic_pct", "۲۵")
        self.assertEqual(saved.await_args.args[1:], ("user_alert_low_traffic_pct", "25"))
        self.assertEqual(saved.await_args.kwargs["reseller_id"], SHOP_ID)

    async def test_hostile_numbers_are_refused(self) -> None:
        for key in ("user_alert_low_traffic_pct", "admin_daily_report_hour", "entry_approval_retry_hours"):
            for raw in ("nan", "-1", "1.5", "99999999", "inf"):
                with self.subTest(key=key, raw=raw):
                    saved, message = await self.save(key, raw)
                    saved.assert_not_awaited()
                    message.answer.assert_awaited_once()

    async def test_unlimited_import_cap_is_stored_as_the_unlimited_value(self) -> None:
        from app.services.service_import_config import UNLIMITED_VALUE

        saved, _ = await self.save("service_import_max_per_user", "نامحدود")
        self.assertEqual(saved.await_args.args[1:], ("service_import_max_per_user", UNLIMITED_VALUE))

    async def test_text_in_a_notify_sub_screen_is_saved_for_the_shop(self) -> None:
        saved, _ = await self.save("user_alert_time_text", "سرویس {service_name} رو به پایان است")
        self.assertEqual(saved.await_args.args[1], "user_alert_time_text")
        self.assertEqual(saved.await_args.kwargs["reseller_id"], SHOP_ID)

    async def test_money_numbers_are_bounded_for_the_shop(self) -> None:
        for key, raw in (("referral_bonus", "-1000"), ("stars_toman_per_star", "0"), ("auto_approve_after_max_amount", "nan")):
            with self.subTest(key=key):
                saved, _ = await self.save(key, raw)
                saved.assert_not_awaited()
        saved, _ = await self.save("referral_bonus", "5000")
        self.assertEqual(saved.await_args.args[1:], ("referral_bonus", "5000"))
        self.assertEqual(saved.await_args.kwargs["reseller_id"], SHOP_ID)

    async def test_forged_key_is_refused(self) -> None:
        saved, message = await self.save("pg_link_watchdog_enabled", "1")
        saved.assert_not_awaited()
        message.answer.assert_awaited_once_with(feedback.INVALID)


class SelectTests(ShopEditHarness):
    async def cycle(self, data: str, current: str = "instant") -> tuple[AsyncMock, MagicMock]:
        callback = MagicMock(data=data, answer=AsyncMock(), message=MagicMock())
        with (
            self.gate(),
            patch.object(reseller_settings, "get_setting", AsyncMock(return_value=current)),
            patch.object(reseller_settings, "set_setting", AsyncMock()) as saved,
            patch.object(reseller_settings, "_render_sub", AsyncMock()),
        ):
            await reseller_settings.settings_select_cycle(callback, MagicMock(), SimpleNamespace(id=1))
        return saved, callback

    async def test_import_mode_cycles_on_the_shops_copy(self) -> None:
        saved, callback = await self.cycle("res:st:sel:service_import_mode")
        self.assertEqual(saved.await_args.args[1:], ("service_import_mode", "approval"))
        self.assertEqual(saved.await_args.kwargs["reseller_id"], SHOP_ID)
        callback.answer.assert_awaited_once_with(feedback.SAVED)

    async def test_select_on_a_non_select_key_is_refused(self) -> None:
        for key in ("billing_on_empty", "user_alert_low_enabled", "nope"):
            with self.subTest(key=key):
                saved, callback = await self.cycle(f"res:st:sel:{key}")
                saved.assert_not_awaited()
                callback.answer.assert_awaited_once_with(feedback.INVALID, show_alert=True)

    async def test_select_without_permission_is_refused(self) -> None:
        callback = MagicMock(data="res:st:sel:service_import_mode", answer=AsyncMock(), message=MagicMock())
        with (
            patch.object(reseller_settings, "_gate", AsyncMock(return_value=(None, "denied"))),
            patch.object(reseller_settings, "set_setting", AsyncMock()) as saved,
        ):
            await reseller_settings.settings_select_cycle(callback, MagicMock(), SimpleNamespace(id=1))
        saved.assert_not_awaited()
        callback.answer.assert_awaited_once_with("denied", show_alert=True)

    async def test_select_field_is_not_opened_as_free_text(self) -> None:
        callback = MagicMock(data="res:st:edit:service_import_mode", answer=AsyncMock(), message=MagicMock())
        with self.gate(), patch("app.bot.nav_input.ask_text", AsyncMock()) as asked:
            await reseller_settings.settings_edit_ask(callback, AsyncMock(), MagicMock(), SimpleNamespace(id=1))
        asked.assert_not_awaited()


class ScreenTests(ShopEditHarness):
    async def test_notify_screen_opens_its_sub_screens(self) -> None:
        callback = MagicMock(message=MagicMock())
        with (
            patch.object(reseller_settings, "get_reseller_profile", AsyncMock(return_value=SimpleNamespace())),
            patch("app.services.authz.resolve_shop_permissions_from_profile", return_value=[]),
            patch("app.services.notifications.get_shop_notify_prefs", AsyncMock(return_value={})),
            patch.object(reseller_settings, "safe_edit_text", AsyncMock()) as edited,
        ):
            await reseller_settings._render_notify(callback, MagicMock(), SHOP_ID)
        markup = edited.await_args.kwargs["reply_markup"]
        callbacks = {b.callback_data for row in markup.inline_keyboard for b in row}
        for sub_id in ("daily", "usage_alerts"):
            self.assertIn(f"res:st:sub:notify:{sub_id}", callbacks)

    async def test_import_screen_shows_the_current_choice(self) -> None:
        with patch.object(reseller_settings, "get_setting", AsyncMock(return_value="approval")):
            panel = await reseller_settings._sub_panel(MagicMock(), "access", "import", SHOP_ID)
        buttons = {b.callback_data: b.text for row in panel[1].inline_keyboard for b in row}
        self.assertIn("res:st:sel:service_import_mode", buttons)
        self.assertIn("با تأیید ادمین", buttons["res:st:sel:service_import_mode"])

    async def test_number_prompt_shows_the_range(self) -> None:
        callback = MagicMock(data="res:st:edit:user_alert_low_time_pct", answer=AsyncMock(), message=MagicMock())
        with (
            self.gate(),
            patch.object(reseller_settings, "get_setting", AsyncMock(return_value="80")),
            patch("app.bot.nav_input.ask_text", AsyncMock()) as asked,
        ):
            await reseller_settings.settings_edit_ask(callback, AsyncMock(), MagicMock(), SimpleNamespace(id=1))
        prompt = asked.await_args.kwargs["prompt"]
        self.assertIn("1", prompt)
        self.assertIn("3650", prompt)


if __name__ == "__main__":
    unittest.main()
