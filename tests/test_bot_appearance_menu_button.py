"""Saving a bot's appearance points only the platform bot's «Open» button at the platform Mini App.

A shop bot signs initData with its own token, so the platform address on a shop bot fails
every Mini App request; its button is left to the shop's own Mini App sync.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import MenuButtonWebApp

from app.services import bot_appearance
from app.services.bot_appearance import BotAppearance, apply_appearance, save_appearance_from_form

PLATFORM_MINIAPP = "https://panel.example.com/miniapp/"
SHOP_ID = 5


def fake_bot() -> SimpleNamespace:
    return SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="shop_bot", first_name="Shop")),
        set_my_name=AsyncMock(),
        set_my_description=AsyncMock(),
        set_my_short_description=AsyncMock(),
        delete_my_commands=AsyncMock(),
        set_chat_menu_button=AsyncMock(),
        delete_chat_menu_button=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )


def miniapp_settings() -> SimpleNamespace:
    return SimpleNamespace(miniapp_enabled=True, miniapp_url=PLATFORM_MINIAPP)


class ApplyAppearanceMenuButtonTests(unittest.IsolatedAsyncioTestCase):
    async def apply(self, *, is_platform_bot: bool) -> SimpleNamespace:
        bot = fake_bot()
        with patch.object(bot_appearance, "Bot", return_value=bot), \
                patch("app.config.get_settings", return_value=miniapp_settings()):
            result = await apply_appearance(
                "1:tok", name="Shop", description="d", short_description="s", is_platform_bot=is_platform_bot,
            )
        self.assertTrue(result.ok)
        return bot

    async def test_shop_bot_appearance_never_sets_the_platform_miniapp_button(self) -> None:
        bot = await self.apply(is_platform_bot=False)
        bot.set_chat_menu_button.assert_not_awaited()
        bot.delete_chat_menu_button.assert_not_awaited()

    async def test_platform_bot_appearance_points_open_at_the_platform_miniapp(self) -> None:
        bot = await self.apply(is_platform_bot=True)
        button = bot.set_chat_menu_button.await_args.kwargs["menu_button"]
        self.assertIsInstance(button, MenuButtonWebApp)
        self.assertEqual(button.web_app.url, PLATFORM_MINIAPP)


class SaveAppearanceBotKindTests(unittest.IsolatedAsyncioTestCase):
    async def saved_as(self, reseller_id: int | None) -> bool:
        apply = AsyncMock(return_value=BotAppearance(ok=True))
        with patch.object(bot_appearance, "apply_appearance", apply), \
                patch("app.services.users.set_settings_bulk", AsyncMock()):
            ok, _msg = await save_appearance_from_form(
                None, {"bot_tg_name": "n"}, token="1:tok", reseller_id=reseller_id,
            )
        self.assertTrue(ok)
        return apply.await_args.kwargs["is_platform_bot"]

    async def test_a_shop_save_is_not_treated_as_the_platform_bot(self) -> None:
        self.assertFalse(await self.saved_as(SHOP_ID))

    async def test_the_platform_save_is_the_platform_bot(self) -> None:
        self.assertTrue(await self.saved_as(None))


if __name__ == "__main__":
    unittest.main()
