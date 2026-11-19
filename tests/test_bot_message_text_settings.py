"""Customer message texts of the web «متن پیام‌ها» tab are editable from the owner and shop bots.

The shop bot edits only its own shop's copy, and never the connection-monitor admin prompts,
which belong to the main bot's Owner.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


from app.bot.handlers import admin_settings, reseller_settings
from app.services import bot_feedback_texts as feedback
from app.services.users import MONITOR_ADMIN_TEXTS_GROUP, SETTING_GROUPS

SHOP_ID = 9
CUSTOMER_TEXT_KEYS = (
    "guide_text",
    "faq_text",
    "empty_services_text",
    "shop_empty_text",
    "delivery_title",
    "payment_ok_title",
    "wallet_success_title",
    "wallet_success_text",
    "connection_status_title",
    "connection_status_empty",
    "connection_status_error",
)
MONITOR_ADMIN_KEYS = tuple(field[0] for field in SETTING_GROUPS[MONITOR_ADMIN_TEXTS_GROUP])


def _web_kind(key: str) -> str:
    return next(field[2] for fields in SETTING_GROUPS.values() for field in fields if field[0] == key)


class FieldCoverageTests(unittest.TestCase):
    def test_owner_bot_edits_every_customer_and_monitor_text_like_the_web(self) -> None:
        for key in (*CUSTOMER_TEXT_KEYS, *MONITOR_ADMIN_KEYS):
            with self.subTest(key=key):
                self.assertIn(key, admin_settings.FIELDS)
                self.assertEqual(admin_settings.FIELDS[key][2], _web_kind(key))

    def test_shop_bot_edits_customer_texts_like_the_web(self) -> None:
        for key in CUSTOMER_TEXT_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, reseller_settings.FIELDS)
                self.assertEqual(reseller_settings.FIELDS[key][2], _web_kind(key))

    def test_shop_bot_never_offers_monitor_admin_prompts(self) -> None:
        for key in MONITOR_ADMIN_KEYS:
            self.assertNotIn(key, reseller_settings.FIELDS)


class ShopScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_shop_save_writes_only_the_shops_own_copy(self) -> None:
        state = AsyncMock(get_data=AsyncMock(return_value={"edit_key": "guide_text"}))
        message = MagicMock(text="راهنمای فروشگاه", entities=None, answer=AsyncMock(), delete=AsyncMock())
        profile = SimpleNamespace(user_id=SHOP_ID)
        with (
            patch.object(reseller_settings, "_gate", AsyncMock(return_value=(profile, None))),
            patch.object(reseller_settings, "set_setting", AsyncMock()) as saved,
            patch.object(reseller_settings, "_close_input_to_location", AsyncMock()),
        ):
            await reseller_settings.settings_edit_save(message, state, MagicMock(), SimpleNamespace(id=1))
        saved.assert_awaited_once()
        self.assertEqual(saved.await_args.args[1], "guide_text")
        self.assertEqual(saved.await_args.kwargs["reseller_id"], SHOP_ID)

    async def test_forged_shop_callback_for_a_monitor_prompt_is_refused(self) -> None:
        callback = MagicMock(
            data="res:st:edit:connection_status_account_prompt", answer=AsyncMock(), message=MagicMock()
        )
        profile = SimpleNamespace(user_id=SHOP_ID)
        state = AsyncMock()
        with (
            patch.object(reseller_settings, "_gate", AsyncMock(return_value=(profile, None))),
            patch("app.bot.nav_input.ask_text", AsyncMock()) as asked,
        ):
            await reseller_settings.settings_edit_ask(callback, state, MagicMock(), SimpleNamespace(id=1))
        asked.assert_not_awaited()
        state.update_data.assert_not_awaited()
        callback.answer.assert_awaited_once_with(feedback.INVALID, show_alert=True)

    async def test_shop_without_settings_permission_cannot_open_a_text(self) -> None:
        callback = MagicMock(data="res:st:edit:guide_text", answer=AsyncMock(), message=MagicMock())
        with (
            patch.object(reseller_settings, "_gate", AsyncMock(return_value=(None, "denied"))),
            patch("app.bot.nav_input.ask_text", AsyncMock()) as asked,
        ):
            await reseller_settings.settings_edit_ask(callback, AsyncMock(), MagicMock(), SimpleNamespace(id=1))
        asked.assert_not_awaited()
        callback.answer.assert_awaited_once_with("denied", show_alert=True)


class ShopSubPanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_screens_list_their_fields(self) -> None:
        with patch.object(reseller_settings, "get_setting", AsyncMock(return_value="")):
            for sub_id, keys in (
                ("guide_texts", ("guide_text", "faq_text", "empty_services_text", "shop_empty_text")),
                ("result_texts", ("delivery_title", "payment_ok_title", "wallet_success_title", "wallet_success_text")),
                ("status_texts", ("connection_status_title", "connection_status_empty", "connection_status_error")),
            ):
                with self.subTest(sub=sub_id):
                    panel = await reseller_settings._sub_panel(MagicMock(), "shop", sub_id, SHOP_ID)
                    self.assertIsNotNone(panel)
                    callbacks = {b.callback_data for row in panel[1].inline_keyboard for b in row}
                    for key in keys:
                        self.assertIn(f"res:st:edit:{key}", callbacks)
                        self.assertLessEqual(len(f"res:st:edit:{key}".encode()), 64)


if __name__ == "__main__":
    unittest.main()
