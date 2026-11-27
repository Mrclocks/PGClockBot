"""The home message's title is the shop name; an empty name shows the welcome text alone."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.handlers.start import render_home

DIVIDER = "━━━━━━━━━━━━"


def _user() -> MagicMock:
    user = MagicMock()
    user.id = 1
    user.full_name = "U"
    user.telegram_id = 10
    user.username = None
    return user


async def _home_text(role: str, shop_title: str) -> str:
    message = AsyncMock()
    ui = {"shop_title": shop_title, "welcome_text": "سلام"}
    profile = MagicMock()
    with (
        patch("app.bot.handlers.start.replace_home", new=AsyncMock()),
        patch("app.bot.menu_nav.build_main_reply_keyboard", new=AsyncMock(return_value=(MagicMock(), ui, None))),
        patch("app.services.reseller_access.load_reseller_actor", new=AsyncMock(return_value=(None, profile))),
        patch("app.bot.handlers.start.kb.reseller_hub_main_keyboard", return_value=MagicMock()),
    ):
        await render_home(
            message, AsyncMock(), _user(), ui=ui, effective_role=role,
            is_reseller_bot=role == "reseller", reseller_owner_id=5 if role == "reseller" else None,
        )
    return message.answer.await_args.args[0]


class HomeShopTitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_empty_shop_name_adds_no_title_for_staff(self) -> None:
        for role in ("admin", "reseller"):
            with self.subTest(role=role):
                text = await _home_text(role, "")
                self.assertNotIn(DIVIDER, text)
                self.assertNotIn("کلاک", text)
                self.assertTrue(text.lstrip("‏").startswith("پنل مدیریت فروشگاه"))

    async def test_a_shop_name_is_the_title(self) -> None:
        for role in ("admin", "reseller"):
            with self.subTest(role=role):
                text = await _home_text(role, "تست ربات")
                self.assertIn("<b>تست ربات</b>", text)
                self.assertIn(DIVIDER, text)


if __name__ == "__main__":
    unittest.main()
