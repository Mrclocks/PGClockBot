"""The bot's Telegram name and descriptions, edited one at a time from the owner and shop bots.

Each save reads the other two texts from Telegram, pushes all three back, and then keeps the
panel's copy in step. A shop changes only its own bot (its own token). Failures show a safe
message, never Telegram's error text, and nothing is stored locally.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.bot.handlers import admin_settings, reseller_settings
from app.services import bot_appearance
from app.services.bot_appearance import (
    PROFILE_APPLY_FAILED,
    PROFILE_NO_TOKEN,
    PROFILE_READ_FAILED,
    BotAppearance,
    update_bot_profile_text,
)

CURRENT = BotAppearance(name="ربات قدیم", description="متن وسط", short_description="درباره", ok=True)
SHOP_ID = 21


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def update(self, *, fetched=CURRENT, applied_ok=True, token="1:abc", key="bot_tg_name", value="ربات تازه"):
        applied = BotAppearance(ok=applied_ok, error="Bad Request: secret internals")
        with (
            patch.object(bot_appearance, "fetch_appearance", AsyncMock(return_value=fetched)),
            patch.object(bot_appearance, "apply_appearance", AsyncMock(return_value=applied)) as apply,
            patch.object(bot_appearance, "set_settings_bulk", AsyncMock()) as stored,
        ):
            await update_bot_profile_text(MagicMock(), token=token, key=key, value=value, reseller_id=SHOP_ID)
        return apply, stored

    async def test_one_text_changes_and_the_others_are_kept(self) -> None:
        apply, stored = await self.update(key="bot_tg_short_description", value="  کپشن تازه  ")
        kwargs = apply.await_args.kwargs
        self.assertEqual((kwargs["name"], kwargs["description"], kwargs["short_description"]), ("ربات قدیم", "متن وسط", "کپشن تازه"))
        self.assertFalse(kwargs["is_platform_bot"])
        self.assertEqual(stored.await_args.kwargs["reseller_id"], SHOP_ID)
        self.assertEqual(stored.await_args.args[1]["bot_tg_short_description"], "کپشن تازه")

    async def test_too_long_text_is_refused_before_any_call(self) -> None:
        with self.assertRaises(ValueError):
            await self.update(value="ا" * 65)

    async def test_missing_token_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            await self.update(token="")
        self.assertEqual(str(caught.exception), PROFILE_NO_TOKEN)

    async def test_unreadable_profile_is_not_overwritten(self) -> None:
        with self.assertRaises(ValueError) as caught:
            await self.update(fetched=BotAppearance(ok=False, error="network detail"))
        self.assertEqual(str(caught.exception), PROFILE_READ_FAILED)

    async def test_refused_update_shows_a_safe_message_and_stores_nothing(self) -> None:
        with (
            patch.object(bot_appearance, "fetch_appearance", AsyncMock(return_value=CURRENT)),
            patch.object(bot_appearance, "apply_appearance", AsyncMock(return_value=BotAppearance(error="Bad Request: x"))),
            patch.object(bot_appearance, "set_settings_bulk", AsyncMock()) as stored,
            self.assertRaises(ValueError) as caught,
        ):
            await update_bot_profile_text(MagicMock(), token="1:abc", key="bot_tg_name", value="x", reseller_id=None)
        self.assertEqual(str(caught.exception), PROFILE_APPLY_FAILED)
        self.assertNotIn("Bad Request", str(caught.exception))
        stored.assert_not_awaited()


class OwnerBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        actor = patch.object(admin_settings, "_ensure_settings_actor", AsyncMock(return_value=True))
        actor.start()
        self.addCleanup(actor.stop)

    async def test_owner_save_pushes_to_the_platform_bot_with_its_token(self) -> None:
        state = AsyncMock(get_data=AsyncMock(return_value={"edit_key": "bot_tg_name"}))
        message = MagicMock(text="کلاک بات", entities=None, answer=AsyncMock(), delete=AsyncMock())
        with (
            patch.object(admin_settings, "_is_admin", return_value=True),
            patch.object(admin_settings, "current_setup_values", return_value={"BOT_TOKEN": "9:platform"}),
            patch.object(admin_settings, "update_bot_profile_text", AsyncMock()) as pushed,
            patch.object(admin_settings, "set_setting", AsyncMock()) as plain,
            patch.object(admin_settings, "_finish_settings_text_edit", AsyncMock()),
        ):
            await admin_settings.settings_edit_save(message, state, MagicMock(), SimpleNamespace(id=1))
        kwargs = pushed.await_args.kwargs
        self.assertEqual((kwargs["token"], kwargs["key"], kwargs["value"], kwargs["reseller_id"]), ("9:platform", "bot_tg_name", "کلاک بات", None))
        plain.assert_not_awaited()

    async def test_owner_sees_the_safe_message_on_failure(self) -> None:
        state = AsyncMock(get_data=AsyncMock(return_value={"edit_key": "bot_tg_description"}))
        message = MagicMock(text="متن", entities=None, answer=AsyncMock(), delete=AsyncMock())
        with (
            patch.object(admin_settings, "_is_admin", return_value=True),
            patch.object(admin_settings, "current_setup_values", return_value={"BOT_TOKEN": "9:platform"}),
            patch.object(admin_settings, "update_bot_profile_text", AsyncMock(side_effect=ValueError(PROFILE_APPLY_FAILED))),
            patch.object(admin_settings, "_finish_settings_text_edit", AsyncMock()) as finished,
        ):
            await admin_settings.settings_edit_save(message, state, MagicMock(), SimpleNamespace(id=1))
        message.answer.assert_awaited_once_with(PROFILE_APPLY_FAILED)
        finished.assert_not_awaited()


class ShopBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_shop_save_uses_only_its_own_bot_token(self) -> None:
        profile = SimpleNamespace(user_id=SHOP_ID, bot_token="sealed-shop-token")
        state = AsyncMock(get_data=AsyncMock(return_value={"edit_key": "bot_tg_short_description"}))
        message = MagicMock(text="فروشگاه ما", entities=None, answer=AsyncMock(), delete=AsyncMock())
        with (
            patch.object(reseller_settings, "_gate", AsyncMock(return_value=(profile, None))),
            patch.object(reseller_settings, "reveal_bot_token", return_value="7:shop") as revealed,
            patch.object(reseller_settings, "update_bot_profile_text", AsyncMock()) as pushed,
            patch.object(reseller_settings, "set_setting", AsyncMock()) as plain,
            patch.object(reseller_settings, "_close_input_to_location", AsyncMock()),
        ):
            await reseller_settings.settings_edit_save(message, state, MagicMock(), SimpleNamespace(id=1))
        revealed.assert_called_once_with("sealed-shop-token")
        kwargs = pushed.await_args.kwargs
        self.assertEqual((kwargs["token"], kwargs["reseller_id"]), ("7:shop", SHOP_ID))
        plain.assert_not_awaited()

    async def test_shop_without_a_bot_gets_the_no_token_message(self) -> None:
        profile = SimpleNamespace(user_id=SHOP_ID, bot_token=None)
        state = AsyncMock(get_data=AsyncMock(return_value={"edit_key": "bot_tg_name"}))
        message = MagicMock(text="نام", entities=None, answer=AsyncMock(), delete=AsyncMock())
        with (
            patch.object(reseller_settings, "_gate", AsyncMock(return_value=(profile, None))),
            patch.object(reseller_settings, "_close_input_to_location", AsyncMock()) as closed,
        ):
            await reseller_settings.settings_edit_save(message, state, MagicMock(), SimpleNamespace(id=1))
        message.answer.assert_awaited_once_with(PROFILE_NO_TOKEN)
        closed.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
