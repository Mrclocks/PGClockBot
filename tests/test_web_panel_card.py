"""The «🌐 وب‌پنل» card replaces the screen it was opened from and «بازگشت» returns there."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.panel_history import panel_history
from app.bot.web_panel_card import OPEN_LABEL, web_panel_card
from tests.chat_sim import ChatSim

ADMIN_PANEL = "🛠 پنل ادمین"
BACK = "بازگشت"


def _backs(message) -> list[str]:
    return [data for row in message.inline for text, data in row if BACK in text]


class CardTests(unittest.TestCase):
    def test_an_absolute_url_gets_an_open_button_and_every_card_a_back_button(self) -> None:
        text, markup = web_panel_card(
            title="t", body="b", url="https://panel.example/settings?a=1&b=2", ui={}, back_callback="nv:adm:settings",
        )
        rows = markup.inline_keyboard
        self.assertEqual((rows[0][0].text, rows[0][0].url), (OPEN_LABEL, "https://panel.example/settings?a=1&b=2"))
        self.assertEqual(rows[-1][0].callback_data, "nv:adm:settings")
        self.assertIn("<code>https://panel.example/settings?a=1&amp;b=2</code>", text)

    def test_a_relative_url_is_shown_without_an_open_button(self) -> None:
        _, markup = web_panel_card(title="t", body="b", url="/settings", ui={}, back_callback="res:st:hub")
        self.assertEqual([[b.callback_data for b in row] for row in markup.inline_keyboard], [["res:st:hub"]])


class PlatformWebPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        panel_history.clear()
        self.addCleanup(panel_history.clear)
        for target, value in (
            ("app.bot.middlewares._rate_limited", MagicMock(return_value=False)),
            ("app.bot.auth.platform_can_manage_representatives", AsyncMock(return_value=True)),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sim = ChatSim(5501)
        await self.sim.start(owner=True)
        self.addAsyncCleanup(self.sim.stop)
        await self.sim.type("/start")
        await self.sim.type(ADMIN_PANEL)
        self.panel = [m for m in self.sim.telegram.visible() if m.from_bot and m.inline][-1]

    def shown(self):
        return self.sim.telegram.messages[self.panel.message_id]

    async def tap(self, data: str) -> None:
        await self.sim._tap(self.shown(), data)

    async def test_the_card_opens_in_the_same_bubble_and_goes_back_to_settings(self) -> None:
        await self.tap("nv:adm:settings")
        settings_text = self.shown().text
        await self.tap("nv:adm:ra:adm_st_panel")
        self.assertIn("وب‌پنل", self.shown().text)
        self.assertEqual(_backs(self.shown()), ["nv:adm:settings"])
        await self.tap("nv:adm:settings")
        self.assertEqual(self.shown().text, settings_text)


class ShopWebPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        panel_history.clear()
        self.addCleanup(panel_history.clear)
        for target, value in (
            ("app.bot.middlewares._rate_limited", MagicMock(return_value=False)),
            ("app.bot.auth.is_bot_owner_principal", AsyncMock(return_value=False)),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sim = ChatSim(5601)
        await self.sim.start_shop(staff=True)
        self.addAsyncCleanup(self.sim.stop)
        await self.sim.type("/start")

    def shown(self):
        return [m for m in self.sim.telegram.visible() if m.from_bot and m.inline][-1]

    async def open(self, data: str) -> None:
        await self.sim._tap([m for m in self.sim.telegram.visible() if m.from_bot][-1], data)

    async def tap(self, data: str) -> None:
        await self.sim._tap(self.shown(), data)

    async def test_shop_settings_card_goes_back_to_the_shop_settings(self) -> None:
        await self.open("res:st:hub")
        await self.tap("nv:res:ra:res_st_panel")
        self.assertIn("فقط همین فروشگاه", self.shown().text)
        self.assertEqual(_backs(self.shown()), ["res:st:hub"])

    async def test_plans_card_goes_back_to_the_plans_screen(self) -> None:
        await self.open("res:plans")
        await self.tap("res:plan:webhint")
        self.assertIn("پلن‌ها", self.shown().text)
        self.assertEqual(_backs(self.shown()), ["res:plans"])
