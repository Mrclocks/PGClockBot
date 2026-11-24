"""Every main-menu button answers: a tour of the real bot for a customer and for the owner.

A cleanup that moves handlers, keyboards or middlewares must keep every button working. The tour
starts the real dispatcher in a simulated private chat, taps each button of the main reply
keyboard (and, for the owner, each inline button of the admin panel it opens) and checks that the bot
answered with a screen of its own and that nothing was logged at ERROR on the way.
"""

from __future__ import annotations

import logging
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.db.models import BotUser, Plan
from tests.chat_sim import ChatSim

RESTART = "🔄 شروع مجدد"
ADMIN_PANEL = "🛠 پنل ادمین"
# Admin-panel buttons that go back to the main menu or switch the menus into edit mode.
LEAVES_PANEL = ("menu:home", "ed:")


class _ErrorLog(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _Tour(unittest.IsolatedAsyncioTestCase):
    owner = False

    async def asyncSetUp(self) -> None:
        self.sim = ChatSim(5101 if self.owner else 6101)
        await self.sim.start(owner=self.owner)
        self.addAsyncCleanup(self.sim.stop)
        for target, value in (
            ("app.bot.middlewares._rate_limited", MagicMock(return_value=False)),
            ("app.bot.auth.platform_can_manage_representatives", AsyncMock(return_value=True)),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        async with self.sim.Session() as session:
            session.add(Plan(id=1, name="بسته اقتصادی", price=100000, duration_days=30, data_limit_gb=10, is_active=True))
            session.add(BotUser(id=60, telegram_id=7100000001, referral_code="t1", full_name="Tour User"))
            await session.commit()
        self.errors = _ErrorLog()
        logging.getLogger().addHandler(self.errors)
        self.addCleanup(logging.getLogger().removeHandler, self.errors)
        await self.sim.type("/start")

    def labels(self) -> list[str]:
        return [label for row in self.sim.telegram.keyboard or [] for label in row]

    async def press(self, label: str) -> None:
        before = {m.message_id for m in self.sim.telegram.visible() if m.from_bot}
        self.errors.records.clear()  # each button is judged on its own errors
        await self.sim.type(label)
        answered = [m for m in self.sim.telegram.visible() if m.from_bot and m.message_id not in before]
        self.assertTrue(answered or self.sim.telegram.keyboard, f"{label!r} got no answer: {self.sim.screen()}")
        self.assertEqual([r.getMessage() for r in self.errors.records], [], f"{label!r} logged an error")

    async def tour(self, labels: list[str], *, back_to: str = "/start") -> None:
        self.assertTrue(labels, "the keyboard has buttons to tour")
        for label in labels:
            if label == RESTART:
                continue
            with self.subTest(button=label):
                await self.press(label)
            await self.sim.type(back_to)


class CustomerMenuTourTests(_Tour):
    owner = False

    async def test_every_customer_menu_button_answers(self) -> None:
        await self.tour(self.labels())


class OwnerMenuTourTests(_Tour):
    owner = True

    async def test_every_owner_menu_button_answers(self) -> None:
        await self.tour(self.labels())

    async def test_every_admin_panel_button_answers(self) -> None:
        await self.sim.type(ADMIN_PANEL)
        hub = self.admin_buttons()
        self.assertTrue(hub, "the admin panel shows its buttons")
        for text, data in hub:
            with self.subTest(button=text):
                await self.sim.type("/start")
                await self.sim.type(ADMIN_PANEL)
                await self.tap_inline(text, data)

    def admin_buttons(self) -> list[tuple[str, str]]:
        """The inline buttons of the admin panel, without the ones that leave it or edit the menus."""
        panel = next(m for m in reversed(self.sim.telegram.visible()) if m.from_bot and m.inline)
        return [(text, data) for row in panel.inline for text, data in row if not data.startswith(LEAVES_PANEL)]

    async def tap_inline(self, text: str, data: str) -> None:
        panel = next(m for m in reversed(self.sim.telegram.visible())
                     if any(d == data for row in m.inline for _, d in row))
        alerts = len(self.sim.telegram.alerts)
        self.errors.records.clear()  # each button is judged on its own errors
        self.sim.telegram.begin_turn()
        await self.sim._tap(panel, data)
        answered = self.sim.telegram.last_turn or len(self.sim.telegram.alerts) > alerts
        self.assertTrue(answered, f"{text!r} got no answer: {self.sim.screen()}")
        self.assertEqual([r.getMessage() for r in self.errors.records], [], f"{text!r} logged an error")

if __name__ == "__main__":
    unittest.main()
