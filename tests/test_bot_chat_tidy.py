"""The bot chat stays tidy: tapped panels are swept and daily reports replace each other."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import EditMessageCaption, EditMessageText
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from app.bot import chat_cleanup, nav_inline
from app.bot.chat_cleanup import ChatCleanupMiddleware, ThrowawayLog
from app.db.models import Setting
from app.services.replaceable_notices import (
    DAILY_REPORT_KEY,
    REPORT_REPLACED_TEXT,
    SUBORDINATE_REPORT_KEY,
    send_replacing_report,
)
from tests.test_chat_cleanup import PANEL, _Chat
from tests.test_replaceable_notices import NoticeCase, fake_bot

BUTTONS = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ بازگشت", callback_data="adm:users")]])


class _CleanupOn(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        patcher = patch.object(chat_cleanup, "_log", ThrowawayLog())
        patcher.start()
        self.addCleanup(patcher.stop)
        token = chat_cleanup._cleanup_on.set(True)
        self.addCleanup(chat_cleanup._cleanup_on.reset, token)

    def tap_on(self, message_id: int, chat_id: int = 100) -> None:
        token = chat_cleanup._tapped.set((chat_id, message_id))
        self.addCleanup(chat_cleanup._tapped.reset, token)

    async def edit(self, chat: _Chat, message_id: int, markup=BUTTONS, *, caption: bool = False) -> None:
        if caption:
            method = EditMessageCaption(chat_id=100, message_id=message_id, caption="c", reply_markup=markup)
        else:
            method = EditMessageText(chat_id=100, message_id=message_id, text="t", reply_markup=markup)
        await chat_cleanup._note_throwaway_requests(AsyncMock(return_value=True), chat.bot, method)

    async def next_panel(self, chat: _Chat) -> None:
        await nav_inline.present_inline_only(chat.message(from_bot=False), text="hub", inline=PANEL)


class TappedPanelTests(_CleanupOn):
    async def test_a_message_sent_outside_the_panel_helpers_goes_once_a_tap_edits_it_into_a_panel(self) -> None:
        chat = _Chat()
        result = chat.message()  # e.g. a search result sent with a plain answer
        self.tap_on(result.message_id)
        await self.edit(chat, result.message_id)
        await self.next_panel(chat)
        self.assertEqual(chat.deleted(), [result.message_id])

    async def test_a_photo_panel_edited_by_caption_is_swept_too(self) -> None:
        chat = _Chat()
        card = chat.message()
        self.tap_on(card.message_id)
        await self.edit(chat, card.message_id, caption=True)
        await self.next_panel(chat)
        self.assertEqual(chat.deleted(), [card.message_id])

    async def test_an_edit_that_removes_the_buttons_leaves_a_record_that_stays(self) -> None:
        chat = _Chat()
        review = chat.message()
        self.tap_on(review.message_id)
        await self.edit(chat, review.message_id)  # «are you sure?» step
        await self.edit(chat, review.message_id, markup=None)  # «✅ approved», no buttons
        await self.next_panel(chat)
        chat.bot.delete_messages.assert_not_awaited()

    async def test_a_panel_kept_after_the_edit_stays(self) -> None:
        chat = _Chat()
        checkout = chat.message()
        self.tap_on(checkout.message_id)
        await self.edit(chat, checkout.message_id)
        chat_cleanup.keep_message(checkout)
        await self.next_panel(chat)
        chat.bot.delete_messages.assert_not_awaited()

    async def test_edits_of_other_messages_and_edits_outside_a_tap_are_ignored(self) -> None:
        chat = _Chat()
        notice = chat.message()
        other = chat.message()
        await self.edit(chat, notice.message_id)  # a job editing a message: no tap
        self.tap_on(other.message_id)
        await self.edit(chat, notice.message_id)  # a sync edit of another staff message
        await self.next_panel(chat)
        chat.bot.delete_messages.assert_not_awaited()

    async def test_switched_off_a_tapped_panel_is_not_noted(self) -> None:
        chat = _Chat()
        result = chat.message()
        self.tap_on(result.message_id)
        chat_cleanup._cleanup_on.set(False)
        await self.edit(chat, result.message_id)
        chat_cleanup._cleanup_on.set(True)
        await self.next_panel(chat)
        chat.bot.delete_messages.assert_not_awaited()


class TapContextTests(unittest.IsolatedAsyncioTestCase):
    def _callback(self, chat_type: str) -> SimpleNamespace:
        message = Message(message_id=42, date=datetime.now(timezone.utc), chat=Chat(id=100, type=chat_type))
        tap = CallbackQuery(
            id="1", from_user=User(id=7, is_bot=False, first_name="A"), chat_instance="c", data="x", message=message
        )
        return SimpleNamespace(callback_query=tap, message=None)

    async def _seen(self, event) -> tuple[int, int] | None:
        seen = []

        async def handler(_event, _data):
            seen.append(chat_cleanup._tapped.get())

        with patch.object(chat_cleanup, "get_all_settings", AsyncMock(return_value={})):
            await ChatCleanupMiddleware()(handler, event, {"session": MagicMock()})
        self.assertIsNone(chat_cleanup._tapped.get())
        return seen[0]

    async def test_a_tap_in_a_private_chat_names_the_tapped_message(self) -> None:
        self.assertEqual(await self._seen(self._callback("private")), (100, 42))

    async def test_group_taps_and_typed_messages_name_nothing(self) -> None:
        self.assertIsNone(await self._seen(self._callback("group")))
        self.assertIsNone(await self._seen(SimpleNamespace(callback_query=None, message=None)))


def _sent(chat_id: int, message_id: int) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=message_id)


class ReportTests(NoticeCase):
    def report_bot(self, bot_id: int = 1) -> MagicMock:
        bot = fake_bot(bot_id)
        ids = iter(range(10, 100))
        bot.send_message = AsyncMock(side_effect=lambda chat_id, *_a, **_k: _sent(chat_id, next(ids)))
        return bot

    async def send(self, bot, chats, key=DAILY_REPORT_KEY) -> int:
        async with self.Session() as session:
            return await send_replacing_report(session, bot, chats, "📊 report", replace_key=key, parse_mode="HTML")

    async def test_todays_report_deletes_yesterdays_in_each_chat(self) -> None:
        bot = self.report_bot()
        self.assertEqual(await self.send(bot, [501, 502]), 2)
        await self.send(bot, [501, 502])
        deleted = sorted((c.kwargs["chat_id"], c.kwargs["message_id"]) for c in bot.delete_message.await_args_list)
        self.assertEqual(deleted, [(501, 10), (502, 11)])
        self.assertEqual(await self.stored(), [(1, 501, 12, DAILY_REPORT_KEY), (1, 502, 13, DAILY_REPORT_KEY)])
        self.assertEqual(bot.send_message.await_args.kwargs["parse_mode"], "HTML")

    async def test_a_report_of_another_kind_or_another_shop_bot_is_left_alone(self) -> None:
        platform, shop = self.report_bot(1), self.report_bot(2)
        await self.send(platform, [501], key=SUBORDINATE_REPORT_KEY)
        await self.send(shop, [501])
        await self.send(platform, [501])
        platform.delete_message.assert_not_awaited()
        shop.delete_message.assert_not_awaited()

    async def test_an_unreachable_chat_is_skipped_and_the_rest_still_get_it(self) -> None:
        bot = self.report_bot()
        send = bot.send_message.side_effect

        def blocked(chat_id, *a, **k):
            if chat_id == 501:
                raise TelegramForbiddenError(MagicMock(), "bot was blocked by the user")
            return send(chat_id, *a, **k)

        bot.send_message.side_effect = blocked
        self.assertEqual(await self.send(bot, [501, 502]), 1)
        self.assertEqual(await self.stored(), [(1, 502, 10, DAILY_REPORT_KEY)])

    async def test_a_report_too_old_to_delete_is_shortened_with_the_report_note(self) -> None:
        from datetime import timedelta

        from app.db.models import BotChatMessage
        from app.services.replaceable_notices import KIND_NOTICE

        async with self.Session() as session:
            session.add(BotChatMessage(bot_id=1, chat_id=501, message_id=3, kind=KIND_NOTICE,
                                       sent_at=datetime.now(timezone.utc) - timedelta(days=3), replace_key=DAILY_REPORT_KEY))
            await session.commit()
        bot = self.report_bot()
        await self.send(bot, [501])
        self.assertEqual(bot.edit_message_text.await_args.kwargs["text"], REPORT_REPLACED_TEXT)


class DailyReportJobTests(NoticeCase):
    async def test_the_owner_daily_report_replaces_the_previous_one(self) -> None:
        from app.jobs import scheduler
        from app.services.users import clear_settings_cache

        hour = str(datetime.now(timezone.utc).hour)
        async with self.Session() as session:
            session.add_all([Setting(key="admin_daily_report_hour", value=hour),
                             Setting(key="subordinate_report_enabled", value="0")])
            await session.commit()
        bot = fake_bot()
        ids = iter([10, 11])
        bot.send_message = AsyncMock(side_effect=lambda chat_id, *_a, **_k: _sent(chat_id, next(ids)))
        clear_settings_cache()
        self.addCleanup(clear_settings_cache)
        with patch.object(scheduler, "SessionLocal", self.Session), \
                patch("app.config.get_settings", return_value=SimpleNamespace(admin_ids=[501])), \
                patch("app.services.daily_report.build_daily_report_outbound", AsyncMock(return_value=("📊", {}))):
            await scheduler.run_admin_daily_report(bot)
            async with self.Session() as session:
                await session.execute(Setting.__table__.delete().where(Setting.key == "admin_daily_report_last"))
                await session.commit()
            clear_settings_cache()
            await scheduler.run_admin_daily_report(bot)
        self.assertEqual(bot.delete_message.await_args.kwargs, {"chat_id": 501, "message_id": 10})
        self.assertEqual(await self.stored(), [(1, 501, 11, DAILY_REPORT_KEY)])


if __name__ == "__main__":
    unittest.main()
