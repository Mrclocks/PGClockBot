"""«نمایش کارت تصادفی»: with several cards one is shown when the switch is on, the customer chooses when it is off."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


from sqlalchemy import select

from app.bot.handlers import shop as shop_h
from app.bot.handlers import wallet as wallet_h
from app.db.models import Setting
from app.services.payment_destinations import enabled_cards, pick_card, random_card_enabled
from app.services.users import DEFAULT_SETTINGS
from tests.test_update_owner_only import OWNER_SESSION, RoutesHarness

CARDS = [
    {"id": "a", "number": "6037000000000001", "holder": "الف", "enabled": True, "sort": 0},
    {"id": "b", "number": "6037000000000002", "holder": "ب", "enabled": True, "sort": 1},
    {"id": "c", "number": "6037000000000003", "holder": "ج", "enabled": False, "sort": 2},
]


def ui_with(**extra) -> dict:
    ui = {"pay_card_enabled": "1", "payment_cards": json.dumps(CARDS), "pay_wallet_enabled": "1", "pay_topup_card_enabled": "1"}
    ui.update(extra)
    return ui


class PickTests(unittest.TestCase):
    def test_the_switch_is_off_by_default(self):
        self.assertFalse(random_card_enabled(DEFAULT_SETTINGS))
        self.assertFalse(random_card_enabled({}))
        self.assertTrue(random_card_enabled({"payment_cards_random": "1"}))

    def test_the_same_seed_always_gives_the_same_card(self):
        cards = enabled_cards(ui_with())
        self.assertEqual({pick_card(cards, "order:7")["id"] for _ in range(20)}, {pick_card(cards, "order:7")["id"]})

    def test_different_orders_spread_over_all_cards(self):
        cards = enabled_cards(ui_with())
        counts = {c["id"]: 0 for c in cards}
        for order_id in range(400):
            counts[pick_card(cards, f"order:{order_id}")["id"]] += 1
        self.assertEqual(set(counts), {"a", "b"})
        self.assertTrue(all(150 <= n <= 250 for n in counts.values()), counts)

    def test_disabled_cards_are_never_offered(self):
        self.assertEqual([c["id"] for c in enabled_cards(ui_with())], ["a", "b"])


class ShopPurchaseTests(unittest.IsolatedAsyncioTestCase):
    async def tap(self, ui: dict, data: str, *, order_id: int = 7):
        callback = MagicMock(data=data)
        callback.answer = AsyncMock()
        callback.message = MagicMock()
        session = MagicMock()
        order = SimpleNamespace(id=order_id, user_id=1, reseller_id=None, amount=50000)
        session.get = AsyncMock(return_value=order)
        shown: dict = {}

        def capture(*_a, **kw):
            shown["card"] = kw.get("card")
            return ("t", {})

        start = AsyncMock(return_value=SimpleNamespace(id=99))
        picker = AsyncMock()
        with (
            patch.object(shop_h, "get_all_settings", AsyncMock(return_value=ui)),
            patch.object(shop_h, "start_card_payment", start),
            patch.object(shop_h, "_await_order_receipt", AsyncMock()),
            patch.object(shop_h, "safe_edit_text", picker),
            patch("app.services.rich_text.outbound_setting_text", capture),
        ):
            await shop_h.pay_card_cb(callback, session, SimpleNamespace(id=1), None)
        return shown, start, picker

    async def test_with_the_switch_off_the_customer_chooses(self):
        shown, start, picker = await self.tap(ui_with(), "pay:card:7")
        picker.assert_awaited_once()
        start.assert_not_awaited()
        self.assertNotIn("card", shown)

    async def test_with_the_switch_on_one_enabled_card_is_shown_without_a_picker(self):
        shown, start, picker = await self.tap(ui_with(payment_cards_random="1"), "pay:card:7")
        picker.assert_not_awaited()
        start.assert_awaited_once()
        self.assertIn(shown["card"], {"6037000000000001", "6037000000000002"})

    async def test_tapping_again_for_the_same_order_shows_the_same_card(self):
        ui = ui_with(payment_cards_random="1")
        first, _s, _p = await self.tap(ui, "pay:card:7")
        second, _s, _p = await self.tap(ui, "pay:card:7")
        self.assertEqual(first["card"], second["card"])

    async def test_different_orders_get_different_cards(self):
        ui = ui_with(payment_cards_random="1")
        seen = {(await self.tap(ui, f"pay:card:{n}", order_id=n))[0]["card"] for n in range(1, 25)}
        self.assertEqual(seen, {"6037000000000001", "6037000000000002"})

    async def test_an_explicit_choice_still_wins(self):
        shown, _start, picker = await self.tap(ui_with(payment_cards_random="1"), "pay:card:7:b")
        picker.assert_not_awaited()
        self.assertEqual(shown["card"], "6037000000000002")

    async def test_a_single_card_is_used_as_before(self):
        only = json.dumps([CARDS[0]])
        shown, start, picker = await self.tap(ui_with(payment_cards=only, payment_cards_random="1"), "pay:card:7")
        picker.assert_not_awaited()
        self.assertEqual(shown["card"], "6037000000000001")


class WalletTopupTests(unittest.IsolatedAsyncioTestCase):
    async def tap(self, ui: dict, data: str, *, amount: int = 50000):
        callback = MagicMock(data=data)
        callback.answer = AsyncMock()
        callback.message = MagicMock()
        state = MagicMock()
        state.get_data = AsyncMock(return_value={"topup_amount": amount})
        state.set_state = AsyncMock()
        state.update_data = AsyncMock()
        shown: dict = {}

        async def instructions(_session, _payment, _method, ui=None, card=None, **_kw):
            shown["card"] = (card or {}).get("number")
            return "text", MagicMock(), {}

        picker = MagicMock(return_value=None)
        with (
            patch("app.services.payment_destinations.inline_picker_markup", picker),
            patch.object(wallet_h, "get_all_settings", AsyncMock(return_value=ui)),
            patch.object(wallet_h, "create_wallet_topup", AsyncMock(return_value=SimpleNamespace(id=5))),
            patch.object(wallet_h, "_topup_instructions", instructions),
            patch.object(wallet_h, "safe_edit_text", AsyncMock()),
            patch.object(wallet_h, "with_cancel_row", MagicMock(return_value=MagicMock()), create=True),
            patch("app.bot.nav_input.remember_prompt", AsyncMock()),
        ):
            try:
                await wallet_h.wtop_choose_method(callback, MagicMock(), state, SimpleNamespace(id=3))
            except Exception:  # noqa: BLE001 - the screen after the card is chosen is not under test
                pass
        return shown, picker

    async def test_the_switch_off_keeps_the_picker(self):
        shown, picker = await self.tap(ui_with(), "wtop:card")
        picker.assert_called_once()
        self.assertNotIn("card", shown)

    async def test_the_switch_on_shows_one_card_stably_for_the_same_amount(self):
        ui = ui_with(payment_cards_random="1")
        first, picker = await self.tap(ui, "wtop:card")
        again, _p = await self.tap(ui, "wtop:card")
        picker.assert_not_called()
        self.assertIn(first["card"], {"6037000000000001", "6037000000000002"})
        self.assertEqual(first["card"], again["card"])


class SettingScreenTests(RoutesHarness):
    async def request(self, method: str, path: str, data: dict | None = None):
        cookie = f"session={self.signer.dumps(OWNER_SESSION)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.request(method, path, headers=headers, data=data)

    async def test_the_owner_sees_the_switch_and_can_save_it(self):
        page = await self.request("GET", "/finance?tab=orders&settings=payment")
        self.assertEqual(page.status_code, 200)
        self.assertIn("s_payment_cards_random", page.text)
        self.assertIn("نمایش کارت تصادفی", page.text)
        saved = await self.request("POST", "/settings?tab=payment", {"s_payment_cards_random": "1"})
        self.assertEqual(saved.status_code, 303)
        from sqlalchemy.ext.asyncio import async_sessionmaker

        async with async_sessionmaker(self.engine, expire_on_commit=False)() as session:
            row = await session.scalar(select(Setting).where(Setting.key == "payment_cards_random"))
        self.assertEqual(row.value, "1")
