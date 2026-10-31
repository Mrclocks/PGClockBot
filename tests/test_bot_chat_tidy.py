"""Each update tracks its own private chat, and daily reports replace each other."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from app.bot import chat_screen
from app.bot.chat_screen import ChatScreenMiddleware
from app.db.models import Setting
from app.services.replaceable_notices import (
    DAILY_REPORT_KEY,
    REPORT_REPLACED_TEXT,
    SUBORDINATE_REPORT_KEY,
    send_replacing_report,
)
from tests.test_replaceable_notices import NoticeCase, fake_bot

BUTTONS = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ بازگشت", callback_data="adm:users")]])


class TapContextTests(unittest.IsolatedAsyncioTestCase):
    def _callback(self, chat_type: str) -> SimpleNamespace:
        message = Message(message_id=42, date=datetime.now(timezone.utc), chat=Chat(id=100, type=chat_type))
        tap = CallbackQuery(
            id="1", from_user=User(id=7, is_bot=False, first_name="A"), chat_instance="c", data="x", message=message
        )
        return SimpleNamespace(callback_query=tap, message=None)

    async def _seen(self, event) -> chat_screen.Turn | None:
        seen = []

        async def handler(_event, _data):
            seen.append(chat_screen.current_turn())

        with patch.object(chat_screen, "get_all_settings", AsyncMock(return_value={})):
            await ChatScreenMiddleware()(handler, event, {"session": MagicMock(), "bot": MagicMock(id=1)})
        self.assertIsNone(chat_screen.current_turn())
        return seen[0]

    async def test_a_tap_in_a_private_chat_tracks_that_chat(self) -> None:
        active = await self._seen(self._callback("private"))
        self.assertEqual(active.key, (1, 100))

    async def test_group_taps_and_updates_without_a_chat_are_not_tracked(self) -> None:
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
