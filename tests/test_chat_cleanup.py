"""A new panel deletes the throwaway messages above it; anything worth keeping stays."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot import chat_cleanup, chat_screen, nav_inline
from app.bot.handlers import shop
from app.bot.chat_cleanup import DELETE_WINDOW_SECONDS, ThrowawayLog
from app.bot.chat_screen import ChatScreenMiddleware
from app.db.models import ResellerSetting, Setting
from app.services.users import DEFAULT_SETTINGS, clear_settings_cache
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

PANEL = InlineKeyboardMarkup(inline_keyboard=[])


class _Chat:
    """One private chat with one bot; ids grow like Telegram's."""

    def __init__(self, *, bot_id: int = 1, chat_id: int = 100) -> None:
        self.bot = MagicMock(id=bot_id, delete_messages=AsyncMock())
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.next_id = 1

    def message(self, *, from_bot: bool = True) -> MagicMock:
        msg = MagicMock(message_id=self.next_id, chat=self.chat, bot=self.bot)
        msg.from_user = SimpleNamespace(is_bot=from_bot)
        self.next_id += 1
        msg.answer = AsyncMock(side_effect=lambda *a, **k: self.message())
        msg.edit_text = AsyncMock()
        return msg

    def deleted(self) -> list[int]:
        return [i for call in self.bot.delete_messages.await_args_list for i in call.kwargs["message_ids"]]


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        patcher = patch.object(chat_cleanup, "_log", ThrowawayLog())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.token = chat_cleanup._cleanup_on.set(True)
        self.addCleanup(chat_cleanup._cleanup_on.reset, self.token)

    async def test_a_new_panel_deletes_the_earlier_panels_and_menu_taps(self) -> None:
        chat = _Chat()
        first = await nav_inline.present_inline_only(chat.message(from_bot=False), text="a", inline=PANEL)
        tap = chat.message(from_bot=False)
        chat_cleanup.note_throwaway(tap)
        second = await nav_inline.present_inline_only(tap, text="b", inline=PANEL)
        self.assertEqual(chat.deleted(), [first.message_id, tap.message_id])
        self.assertNotIn(second.message_id, chat.deleted())

    async def test_messages_that_were_never_noted_are_never_deleted(self) -> None:
        chat = _Chat()
        delivery = chat.message()  # sent by the bot outside the panel helpers
        await nav_inline.present_inline_only(chat.message(from_bot=False), text="a", inline=PANEL)
        await nav_inline.present_inline_only(chat.message(from_bot=False), text="b", inline=PANEL)
        self.assertNotIn(delivery.message_id, chat.deleted())

    async def test_a_kept_panel_survives(self) -> None:
        chat = _Chat()
        receipt = await nav_inline.present_inline_only(chat.message(from_bot=False), text="pay", inline=PANEL)
        chat_cleanup.keep_message(receipt)
        await nav_inline.present_inline_only(chat.message(from_bot=False), text="b", inline=PANEL)
        self.assertEqual(chat.deleted(), [])

    async def test_payment_instructions_stay_while_the_person_pays(self) -> None:
        chat = _Chat()
        panel = await nav_inline.present_inline_only(chat.message(from_bot=False), text="checkout", inline=PANEL)
        with patch.object(shop, "safe_edit_text", AsyncMock()):
            await shop._await_order_receipt(MagicMock(message=panel), MagicMock(), MagicMock(), title="💳", body="6037")
        await nav_inline.present_inline_only(chat.message(from_bot=False), text="b", inline=PANEL)
        self.assertNotIn(panel.message_id, chat.deleted())

    async def test_the_message_carrying_the_menu_keyboard_is_never_noted(self) -> None:
        chat = _Chat()
        menu = chat.message()
        chat_cleanup.note_throwaway(menu, reply_markup=ReplyKeyboardMarkup(keyboard=[]))
        await nav_inline.present_inline_only(chat.message(from_bot=False), text="b", inline=PANEL)
        self.assertEqual(chat.deleted(), [])

    async def test_editing_the_panel_in_place_clears_what_is_above_it(self) -> None:
        chat = _Chat()
        panel = await nav_inline.present_inline_only(chat.message(from_bot=False), text="a", inline=PANEL)
        tap = chat.message(from_bot=False)
        chat_cleanup.note_throwaway(tap)
        await nav_inline.present_inline_only(panel, text="b", inline=PANEL)
        self.assertEqual(chat.deleted(), [tap.message_id])

    async def test_nothing_is_noted_or_deleted_while_switched_off(self) -> None:
        chat_cleanup._cleanup_on.set(False)
        chat = _Chat()
        await nav_inline.present_inline_only(chat.message(from_bot=False), text="a", inline=PANEL)
        await nav_inline.present_inline_only(chat.message(from_bot=False), text="b", inline=PANEL)
        chat.bot.delete_messages.assert_not_awaited()

    async def test_another_chat_or_another_bot_is_never_touched(self) -> None:
        mine, other_chat, other_bot = _Chat(), _Chat(chat_id=200), _Chat(bot_id=2)
        await nav_inline.present_inline_only(other_chat.message(from_bot=False), text="x", inline=PANEL)
        await nav_inline.present_inline_only(other_bot.message(from_bot=False), text="x", inline=PANEL)
        await nav_inline.present_inline_only(mine.message(from_bot=False), text="a", inline=PANEL)
        await nav_inline.present_inline_only(mine.message(from_bot=False), text="b", inline=PANEL)
        self.assertEqual(len(mine.deleted()), 1)
        other_chat.bot.delete_messages.assert_not_awaited()
        other_bot.bot.delete_messages.assert_not_awaited()

    async def test_group_chats_are_left_alone(self) -> None:
        chat = _Chat()
        chat.chat.type = "group"
        await nav_inline.present_inline_only(chat.message(from_bot=False), text="a", inline=PANEL)
        await nav_inline.present_inline_only(chat.message(from_bot=False), text="b", inline=PANEL)
        chat.bot.delete_messages.assert_not_awaited()

    async def test_a_refused_delete_does_not_break_the_screen(self) -> None:
        chat = _Chat()
        chat.bot.delete_messages.side_effect = TelegramBadRequest(method=MagicMock(), message="message can't be deleted")
        await nav_inline.present_inline_only(chat.message(from_bot=False), text="a", inline=PANEL)
        shown = await nav_inline.present_inline_only(chat.message(from_bot=False), text="b", inline=PANEL)
        self.assertIsNotNone(shown)

    async def test_the_tracked_panel_edit_path_clears_too(self) -> None:
        chat = _Chat()
        state = AsyncMock()
        state.get_data.return_value = {}
        first = await nav_inline.present_nav_panel(chat.message(from_bot=False), text="a", inline=PANEL, state=state)
        state.get_data.return_value = {nav_inline.NAV_PANEL_CHAT_KEY: 100, nav_inline.NAV_PANEL_MSG_KEY: first.message_id}
        tap = chat.message(from_bot=False)
        chat_cleanup.note_throwaway(tap)
        chat.bot.edit_message_text = AsyncMock()
        await nav_inline.present_nav_panel(tap, text="b", inline=PANEL, state=state)
        self.assertEqual(chat.deleted(), [tap.message_id])



MENU = ReplyKeyboardMarkup(keyboard=[])


class KeyboardMessageTests(unittest.IsolatedAsyncioTestCase):
    """Only the newest message carrying the menu keyboard stays; a home sweeps temporary messages."""

    def setUp(self) -> None:
        for name in ("_log", "_homes"):
            patcher = patch.object(chat_cleanup, name, ThrowawayLog())
            patcher.start()
            self.addCleanup(patcher.stop)
        self.token = chat_cleanup._cleanup_on.set(True)
        self.addCleanup(chat_cleanup._cleanup_on.reset, self.token)

    async def test_a_new_keyboard_message_deletes_the_older_ones(self) -> None:
        chat = _Chat()
        first, second, third = chat.message(), chat.message(), chat.message()
        for message in (first, second, third):
            await chat_cleanup.replace_carrier(message)
        self.assertEqual(sorted(chat.deleted()), [first.message_id, second.message_id])

    async def test_a_delivery_carrying_the_keyboard_replaces_older_ones_but_stays_itself(self) -> None:
        chat = _Chat()
        home, delivery, later = chat.message(), chat.message(), chat.message()
        await chat_cleanup.replace_carrier(home)
        await chat_cleanup.replace_carrier(delivery, track=False)
        await chat_cleanup.replace_carrier(later)
        self.assertEqual(chat.deleted(), [home.message_id])

    async def test_a_home_sweeps_the_temporary_messages_but_not_itself(self) -> None:
        chat = _Chat()
        panel = await nav_inline.present_inline_only(chat.message(from_bot=False), text="a", inline=PANEL)
        typed = chat.message(from_bot=False)
        chat_cleanup.note_throwaway(typed)
        home = chat.message()
        await chat_cleanup.replace_home(home, sweep=True)
        self.assertEqual(sorted(chat.deleted()), sorted([panel.message_id, typed.message_id]))

    async def test_keyboard_messages_stay_while_switched_off(self) -> None:
        chat = _Chat()
        chat_cleanup._cleanup_on.set(False)
        await chat_cleanup.replace_carrier(chat.message())
        await chat_cleanup.replace_carrier(chat.message())
        self.assertEqual(chat.deleted(), [])


class LogTests(unittest.TestCase):
    def test_messages_too_old_to_delete_are_dropped(self) -> None:
        log = ThrowawayLog()
        log.add((1, 1), 5, 0.0)
        log.add((1, 1), 6, DELETE_WINDOW_SECONDS)
        self.assertEqual(log.take_except((1, 1), None, DELETE_WINDOW_SECONDS + 1), [6])
        self.assertEqual(log.take_except((1, 1), None, DELETE_WINDOW_SECONDS + 1), [])

    def test_the_log_is_bounded(self) -> None:
        log = ThrowawayLog(per_chat=3, chats=2)
        for mid in range(10):
            log.add((1, 1), mid, 0.0)
        self.assertEqual(log.take_except((1, 1), None, 1.0), [7, 8, 9])
        for chat in range(5):
            log.add((1, chat), 1, 0.0)
        self.assertEqual([log.take_except((1, chat), None, 1.0) for chat in range(5)], [[], [], [], [1], [1]])


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, ui: dict[str, str], text: str) -> tuple[bool, ThrowawayLog]:
        log = ThrowawayLog()
        seen: list[bool] = []
        message = MagicMock(spec=["text", "entities", "chat", "bot", "message_id"])
        message.text, message.entities, message.message_id = text, None, 9
        message.chat, message.bot = SimpleNamespace(id=100, type="private"), MagicMock(id=1)

        async def handler(_event, _data):
            seen.append(chat_cleanup.is_cleanup_on())

        with (
            patch.object(chat_cleanup, "_log", log),
            patch.object(chat_screen, "get_all_settings", AsyncMock(return_value=ui)),
            patch.object(chat_screen, "Message", MagicMock),
        ):
            data = {"session": MagicMock(), "bot": MagicMock(id=1)}
            await ChatScreenMiddleware()(handler, SimpleNamespace(message=message), data)
        self.assertFalse(chat_cleanup.is_cleanup_on())
        return seen[0], log

    async def test_the_switch_follows_this_bots_setting(self) -> None:
        on, _ = await self._run({"chat_cleanup_enabled": "1"}, "hello")
        unset, _ = await self._run({}, "hello")
        off, _ = await self._run({"chat_cleanup_enabled": "0"}, "hello")
        self.assertEqual((on, unset, off), (True, True, False))
        self.assertEqual(DEFAULT_SETTINGS["chat_cleanup_enabled"], "1")

    async def test_menu_taps_and_typed_text_are_both_temporary(self) -> None:
        ui = {"chat_cleanup_enabled": "1", "btn_wallet": "💰 کیف پول"}
        _, tapped = await self._run(ui, "💰 کیف پول")
        _, typed = await self._run(ui, "سلام")
        self.assertEqual(tapped.take_except((1, 100), None, 1.0), [9])
        self.assertEqual(typed.take_except((1, 100), None, 1.0), [9])

    async def test_nothing_is_noted_while_switched_off(self) -> None:
        _, log = await self._run({"chat_cleanup_enabled": "0"}, "سلام")
        self.assertEqual(log.take_except((1, 100), None, 1.0), [])


class SettingTests(RoutesHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        clear_settings_cache()
        self.addCleanup(clear_settings_cache)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def request(self, session_payload: dict, path: str, data: dict):
        cookie = f"session={self.signer.dumps(session_payload)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.request("POST", path, headers=headers, data=data)

    async def test_the_platform_and_each_shop_switch_it_separately(self) -> None:
        await self.request(OWNER_SESSION, "/settings?tab=menu", {"menu_order": "shop", "chat_cleanup_enabled": "1"})
        await self.request(SHOP_SESSION, "/shop-settings/menu-layout", {"menu_order": "shop"})
        async with self.Session() as session:
            platform = await session.scalar(select(Setting.value).where(Setting.key == "chat_cleanup_enabled"))
            shop = await session.scalar(
                select(ResellerSetting.value).where(
                    ResellerSetting.reseller_user_id == 5, ResellerSetting.key == "chat_cleanup_enabled"
                )
            )
        self.assertEqual((platform, shop), ("1", "0"))


if __name__ == "__main__":
    unittest.main()
