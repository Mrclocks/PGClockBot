"""«🎯 مخاطبان هدفمند» in the bot: the owner and a shop pick a narrower segment, and a forged one is refused."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import update

from app.bot.broadcast_segments import (
    HUB_SEGMENTS,
    PLATFORM_LIST,
    PLATFORM_PICK,
    SHOP_LIST,
    SHOP_PICK,
    targeted_keyboard,
)
from app.bot.panel_history import panel_history
from app.db.models import BotUser, ResellerProfile
from app.services.broadcast_audience import SEGMENTS
from app.services.notifications import _send_to_chats
from tests.chat_sim import ChatSim


def _data(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class KeyboardTests(unittest.TestCase):
    def test_the_owner_list_has_every_narrow_segment_and_fits_telegram(self) -> None:
        data = _data(targeted_keyboard(shop_id=None, pick_prefix=PLATFORM_PICK, back="nv:adm:broadcast", ui={}))
        picks = {d[len(PLATFORM_PICK):] for d in data if d.startswith(PLATFORM_PICK)}
        self.assertEqual(picks, set(SEGMENTS) - HUB_SEGMENTS)
        self.assertTrue(all(len(d.encode()) <= 64 for d in data))
        self.assertEqual(data[-1], "nv:adm:broadcast")

    def test_a_shop_list_never_offers_platform_segments(self) -> None:
        data = _data(targeted_keyboard(shop_id=7, pick_prefix=SHOP_PICK, back="shp:bc:hub", ui={}))
        for key in ("users", "resellers", "admins", "all"):
            self.assertNotIn(f"{SHOP_PICK}{key}", data)


class _Flow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        panel_history.clear()
        self.addCleanup(panel_history.clear)
        patcher = patch("app.bot.middlewares._rate_limited", MagicMock(return_value=False))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def show(self, label: str, data: str) -> None:
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=data)]])
        await _send_to_chats(self.sim.bot, [self.sim.telegram_id], "📢 پیام گروهی", markup=markup)


class OwnerFlowTests(_Flow):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.sim = ChatSim(5401)
        await self.sim.start(owner=True)
        self.addAsyncCleanup(self.sim.stop)
        async with self.sim.Session() as session:
            session.add(BotUser(id=1, telegram_id=5401, referral_code="own", role="admin"))
            await session.commit()

    async def test_the_owner_picks_a_segment_and_is_asked_for_the_text(self) -> None:
        await self.show("🎯 مخاطبان هدفمند", PLATFORM_LIST)
        await self.sim.tap("🎯 مخاطبان هدفمند")
        await self.sim.tap("نزدیک انقضا")
        screen = "\n".join(m.text for m in self.sim.telegram.visible())
        self.assertIn("مخاطب: <b>نزدیک انقضا · 3 روز</b>", screen.replace("&lt;", "<"))

    async def test_a_forged_segment_is_refused(self) -> None:
        await self.show("x", f"{PLATFORM_PICK}nobody")
        await self.sim.tap("x")
        self.assertTrue(self.sim.telegram.alerts)


class ShopFlowTests(_Flow):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.sim = ChatSim(5402)
        await self.sim.start_shop(staff=True)
        self.addAsyncCleanup(self.sim.stop)
        # The shop permission gate has its own tests; here the shop is resolved as the simulated shop.
        resolver = patch("app.bot.handlers.shop_broadcast.resolve_shop_id", AsyncMock(return_value=self.sim.shop_owner_id))
        resolver.start()
        self.addCleanup(resolver.stop)

    async def test_the_shop_lists_its_segments_and_refuses_a_platform_one(self) -> None:
        await self.show("🎯", SHOP_LIST)
        await self.sim.tap("🎯")
        shown = [d for m in self.sim.telegram.visible() for row in m.inline for _t, d in row]
        self.assertIn(f"{SHOP_PICK}expiring", shown)
        alerts = len(self.sim.telegram.alerts)
        await self.show("forged", f"{SHOP_PICK}admins")
        await self.sim.tap("forged")
        self.assertGreater(len(self.sim.telegram.alerts), alerts)


class ShopHubTests(_Flow):
    """The shop owner opens group messages through the real permission check (nothing stubbed)."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.sim = ChatSim(5403)
        await self.sim.start_shop(staff=True)
        self.addAsyncCleanup(self.sim.stop)

    async def grant(self, permissions: str) -> None:
        async with self.sim.Session() as session:
            await session.execute(update(ResellerProfile).values(bot_permissions=permissions, web_permissions=permissions))
            await session.commit()

    async def test_the_shop_owner_opens_group_messages_without_an_error(self) -> None:
        from app.bot.handlers.shop_broadcast import CB_HUB

        await self.grant("dashboard,broadcast,campaigns")
        await self.show("📢 پیام گروهی", CB_HUB)
        await self.sim.tap("📢 پیام گروهی")
        screen = self.sim.screen()
        self.assertFalse(any("خطایی رخ داد" in line for line in screen), screen)
        shown = [d for m in self.sim.telegram.visible() for row in m.inline for _t, d in row]
        self.assertIn(SHOP_LIST, shown)
        self.assertIn(f"{SHOP_PICK}all", shown)

    async def test_staff_without_the_permission_is_told_so(self) -> None:
        from app.bot.handlers.shop_broadcast import CB_HUB

        await self.grant("dashboard")
        await self.show("📢 پیام گروهی", CB_HUB)
        await self.sim.tap("📢 پیام گروهی")
        self.assertFalse(any("خطایی رخ داد" in line for line in self.sim.screen()), self.sim.screen())
        self.assertTrue(self.sim.telegram.alerts)


if __name__ == "__main__":
    unittest.main()
