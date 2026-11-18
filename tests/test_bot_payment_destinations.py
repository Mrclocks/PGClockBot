"""Card, link-gateway and crypto-wallet lists edited from the owner and shop bots.

Values are validated like the web form (and stricter for links: https only), a list is capped,
duplicates are refused, and each shop changes only its own list.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import payment_destinations_editor as editor
from app.bot.handlers import admin_settings, reseller_settings
from app.db.models import Base, BotUser
from app.services import bot_feedback_texts as feedback
from app.services import payment_destination_edits as edits
from app.services.payment_destinations import MAX_ITEMS, get_payment_cards
from app.services.users import clear_settings_cache, get_setting

SHOP_A, SHOP_B = 51, 52
CARD = "6037997512345678"


class CleanFieldTests(unittest.TestCase):
    def test_valid_values(self) -> None:
        self.assertEqual(edits.clean_field("c", "number", "6037-9975-1234-5678"), CARD)
        self.assertEqual(edits.clean_field("w", "asset", "usdt"), "USDT")
        self.assertEqual(edits.clean_field("w", "network", "trc20"), "TRC20")
        self.assertEqual(
            edits.clean_field("g", "link", "https://pay.example.com/?a={amount}"), "https://pay.example.com/?a={amount}"
        )

    def test_hostile_values_are_refused(self) -> None:
        cases = {
            ("c", "number"): ("1234", "abcd", ""),
            ("g", "name"): ("",),
            ("g", "link"): ("http://pay.example.com", "javascript:{alert(1)}", "tg://x", "https://a b", ""),
            ("w", "asset"): ("U", "<b>"),
            ("w", "network"): ("x", "TRC 20"),
            ("w", "address"): ("short", "has space in it", "<script>alert(1)</script>"),
        }
        for (kind, field), values in cases.items():
            for raw in values:
                with self.subTest(kind=kind, field=field, raw=raw), self.assertRaises(ValueError):
                    edits.clean_field(kind, field, raw)


class ListEditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as session:
            session.add_all([
                BotUser(id=SHOP_A, telegram_id=9051, referral_code="pa", role="reseller"),
                BotUser(id=SHOP_B, telegram_id=9052, referral_code="pb", role="reseller"),
            ])
            await session.commit()
        clear_settings_cache()
        self.addCleanup(clear_settings_cache)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_add_toggle_move_delete_on_the_shops_own_list(self) -> None:
        async with self.sessions() as session:
            await edits.add_item(session, "c", SHOP_A, {"number": CARD, "holder": "علی"})
            items = await edits.add_item(session, "c", SHOP_A, {"number": "6219861012345678", "holder": "رضا"})
            self.assertEqual([i["number"] for i in items], [CARD, "6219861012345678"])
            items = await edits.move_item(session, "c", SHOP_A, items[1]["id"], up=True)
            self.assertEqual(items[0]["number"], "6219861012345678")
            items = await edits.toggle_item(session, "c", SHOP_A, items[0]["id"])
            self.assertFalse(items[0]["enabled"])
            items = await edits.delete_item(session, "c", SHOP_A, items[0]["id"])
            self.assertEqual([i["number"] for i in items], [CARD])
            # The legacy first-entry keys follow the list.
            self.assertEqual(await get_setting(session, "card_number", reseller_id=SHOP_A), CARD)
            self.assertEqual(await get_payment_cards(session, reseller_id=SHOP_B), [])

    async def test_another_shop_cannot_touch_an_item_by_id(self) -> None:
        async with self.sessions() as session:
            items = await edits.add_item(session, "c", SHOP_A, {"number": CARD, "holder": ""})
            with self.assertRaises(ValueError):
                await edits.delete_item(session, "c", SHOP_B, items[0]["id"])
            self.assertEqual(len(await get_payment_cards(session, reseller_id=SHOP_A)), 1)

    async def test_duplicates_and_a_full_list_are_refused(self) -> None:
        async with self.sessions() as session:
            await edits.add_item(session, "c", SHOP_A, {"number": CARD, "holder": ""})
            with self.assertRaises(ValueError):
                await edits.add_item(session, "c", SHOP_A, {"number": CARD, "holder": "x"})
            for i in range(1, MAX_ITEMS):
                await edits.add_item(session, "c", SHOP_A, {"number": f"60379975{i:08d}", "holder": ""})
            with self.assertRaises(ValueError) as caught:
                await edits.add_item(session, "c", SHOP_A, {"number": "6219861099999999", "holder": ""})
            self.assertEqual(str(caught.exception), edits.FULL_LIST)


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def callback(self, data: str) -> MagicMock:
        return MagicMock(data=data, answer=AsyncMock(), message=MagicMock())

    async def test_shop_without_settings_permission_is_refused(self) -> None:
        callback = self.callback("pde:r:c:l")
        with (
            patch.object(editor, "shop_settings_shop_id", AsyncMock(return_value=None)),
            patch.object(edits, "list_items", AsyncMock()) as listed,
        ):
            await editor.shop_destinations(callback, MagicMock(), SimpleNamespace(id=1), AsyncMock())
        listed.assert_not_awaited()
        callback.answer.assert_awaited_once_with(feedback.NO_SHOP_SETTINGS_ACCESS, show_alert=True)

    async def test_shop_actions_use_the_authenticated_shop(self) -> None:
        callback = self.callback("pde:r:c:X:abc123")
        with (
            patch.object(editor, "shop_settings_shop_id", AsyncMock(return_value=SHOP_A)),
            patch.object(edits, "list_items", AsyncMock(return_value=[])),
            patch.object(edits, "delete_item", AsyncMock(return_value=[])) as deleted,
            patch.object(editor, "safe_edit_text", AsyncMock()),
        ):
            await editor.shop_destinations(callback, MagicMock(), SimpleNamespace(id=1), AsyncMock())
        self.assertEqual(deleted.await_args.args[1:], ("c", SHOP_A, "abc123"))

    async def test_unknown_kind_or_view_is_refused(self) -> None:
        for data in ("pde:a:z:l", "pde:a:c:zz", "pde:a"):
            callback = self.callback(data)
            with self.subTest(data=data), patch.object(edits, "list_items", AsyncMock(return_value=[])):
                await editor.owner_destinations(callback, MagicMock(), AsyncMock())
                callback.answer.assert_awaited_once_with(feedback.INVALID, show_alert=True)

    async def test_add_flow_asks_each_field_then_saves(self) -> None:
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={editor._STATE_KEY: {"kind": "w", "step": 2, "values": {"asset": "USDT", "network": "TRC20"}}})
        message = MagicMock(text="TXyz1234567890abcdef", answer=AsyncMock())
        with (
            patch.object(edits, "add_item", AsyncMock(return_value=[])) as added,
            patch.object(editor, "finish_text_step", AsyncMock()),
        ):
            await editor.handle_message(message, MagicMock(), state, editor.Actor("r", SHOP_A))
        self.assertEqual(added.await_args.args[1:], ("w", SHOP_A, {"asset": "USDT", "network": "TRC20", "address": "TXyz1234567890abcdef"}))

    async def test_a_bad_value_keeps_the_prompt(self) -> None:
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={editor._STATE_KEY: {"kind": "c", "step": 0, "values": {}}})
        message = MagicMock(text="123", answer=AsyncMock())
        with patch.object(edits, "add_item", AsyncMock()) as added:
            await editor.handle_message(message, MagicMock(), state, editor.Actor("a", None))
        added.assert_not_awaited()
        message.answer.assert_awaited_once()
        state.set_state.assert_not_awaited()

    def test_views_escape_stored_values_and_fit_callback_limits(self) -> None:
        actor = editor.Actor("r", SHOP_A)
        item = {"id": "a" * 12, "name": "<b>x</b>", "link": "https://e.com/<i>", "enabled": True}
        text, markup = editor.item_view(actor, "g", item)
        self.assertNotIn("<b>x</b>", text)
        self.assertIn("&lt;b&gt;", text)
        for row in markup.inline_keyboard:
            for button in row:
                self.assertLessEqual(len(button.callback_data.encode()), 64)

    def test_settings_subsections_link_the_lists(self) -> None:
        for kind, sub in editor.SETTINGS_SUB.items():
            for sections in (admin_settings.SECTIONS, reseller_settings.SECTIONS):
                subs = {s[0] for s in sections["pay"]["subs"]}
                self.assertIn(sub, subs, kind)


if __name__ == "__main__":
    unittest.main()


class WiringTests(unittest.TestCase):
    def test_owner_editor_is_behind_the_owner_gate_and_blocked_on_shop_bots(self) -> None:
        from app.bot import create_dispatcher

        dp = create_dispatcher()
        names = {type(m).__name__ for m in editor.owner_router.callback_query.middleware}
        self.assertIn("_RequireBotOwnerPrincipal", names)
        self.assertIn("_BlockPlatformAdminOnResellerBot", names)
        message_names = {type(m).__name__ for m in editor.owner_router.message.middleware}
        self.assertIn("_RequireBotOwnerPrincipal", message_names)
        self.assertIsNotNone(dp)

    def test_settings_screens_show_the_list_entry(self) -> None:
        for kind, sub in editor.SETTINGS_SUB.items():
            for for_reseller in (False, True):
                rows = editor.destination_list_rows("pay", sub, for_reseller=for_reseller)
                self.assertEqual(rows[0][0].callback_data, f"pde:{'r' if for_reseller else 'a'}:{kind}:l")
        self.assertEqual(editor.destination_list_rows("pay", "psp", for_reseller=False), [])
        self.assertEqual(editor.destination_list_rows("access", "card", for_reseller=False), [])
