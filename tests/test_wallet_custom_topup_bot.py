"""A custom wallet top-up amount typed in the bot reaches the payment unchanged."""

from __future__ import annotations

import json
import unittest

from sqlalchemy import select

from app.db.models import Payment
from app.services.users import set_setting
from app.services.wallet_topup import TopupError, parse_typed_topup_amount
from tests.chat_sim import ChatSim

CARD = {"id": "c1", "number": "6037991111111111", "holder": "Shop", "enabled": True}


class CustomTopupAmountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.sim = ChatSim(5811)
        await self.sim.start(owner=False)
        self.addAsyncCleanup(self.sim.stop)
        async with self.sim.Session() as session:
            await set_setting(session, "pay_card_enabled", "1")
            await set_setting(session, "payment_cards", json.dumps([CARD]))
            await set_setting(session, "wallet_topup_presets", "50000,100000")
            await session.commit()
        await self.sim.type("/start")

    async def _payments(self) -> list[Payment]:
        async with self.sim.Session() as session:
            return list((await session.scalars(select(Payment))).all())

    async def _open_topup(self) -> None:
        keyboard = self.sim.telegram.keyboard_carriers()[-1].keyboard
        wallet = next(label for row in keyboard for label in row if "کیف پول" in label)
        await self.sim.type(wallet)
        await self.sim.tap("شارژ")

    def _first_preset_label(self) -> str:
        return self.sim.telegram.visible()[-1].inline[0][0][0]

    async def test_a_typed_amount_is_the_amount_charged_by_card(self) -> None:
        await self._open_topup()
        await self.sim.tap("مبلغ دیگر")
        await self.sim.type("۷۳٬۰۰۰")
        await self.sim.tap("کارت")

        payments = await self._payments()
        self.assertEqual([p.amount for p in payments], [73000], self.sim.screen())

    async def test_an_amount_typed_after_picking_a_preset_replaces_it(self) -> None:
        await self._open_topup()
        await self.sim.tap(self._first_preset_label())
        await self.sim.type("73000")
        await self.sim.tap("کارت")

        payments = await self._payments()
        self.assertEqual([p.amount for p in payments], [73000], self.sim.screen())

    async def test_other_text_on_the_methods_screen_keeps_the_method_buttons(self) -> None:
        await self._open_topup()
        await self.sim.tap(self._first_preset_label())
        await self.sim.type("سلام")
        await self.sim.tap("کارت")

        payments = await self._payments()
        self.assertEqual([p.amount for p in payments], [50000], self.sim.screen())

    async def test_an_amount_above_the_limit_is_refused_at_the_amount_step(self) -> None:
        await self._open_topup()
        await self.sim.tap("مبلغ دیگر")
        await self.sim.type("600000000")

        self.assertNotIn("کارت", " ".join(self.sim.telegram.visible()[-1].buttons()))
        self.assertIn("بین", self.sim.telegram.visible()[-1].text)
        self.assertEqual(await self._payments(), [])


class TypedAmountParsingTests(unittest.TestCase):
    def test_persian_digits_and_grouping_are_read(self) -> None:
        self.assertEqual(parse_typed_topup_amount(" ۷۳٬۰۰۰ "), 73000)
        self.assertEqual(parse_typed_topup_amount("1,500,000"), 1500000)

    def test_out_of_range_or_malformed_amounts_are_refused(self) -> None:
        for raw in ("", None, "999", "500000001", "12.5", "-5000", "abc", "۱e۵", "9" * 13):
            with self.subTest(raw=raw), self.assertRaises(TopupError):
                parse_typed_topup_amount(raw)
