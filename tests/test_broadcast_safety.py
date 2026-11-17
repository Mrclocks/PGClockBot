"""Platform broadcasts: text Telegram accepts, one broadcast per submit, and preview before sending."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import admin_broadcast_confirm as confirm
from app.db import Base
from app.db.models import BotUser, BroadcastLog
from app.services.broadcast_queue import new_request_token, queue_broadcast
from app.services.telegram_html import normalize_telegram_html


class NormalizeTests(unittest.TestCase):
    def test_stray_signs_become_text(self) -> None:
        self.assertEqual(normalize_telegram_html("قیمت < ۱۰۰ & > ۵۰"), "قیمت &lt; ۱۰۰ &amp; &gt; ۵۰")

    def test_supported_tags_and_entities_are_kept(self) -> None:
        text = '<b>تخفیف</b> <a href="https://t.me/x">کانال</a> &amp; &#128512;'
        self.assertEqual(normalize_telegram_html(text), text)

    def test_unsupported_tags_and_entities_are_shown_as_text(self) -> None:
        self.assertEqual(normalize_telegram_html("<script>x</script> &nbsp;"), "&lt;script&gt;x&lt;/script&gt; &amp;nbsp;")

    def test_unbalanced_tags_are_refused(self) -> None:
        for text in ("<b>open", "close</b>", "<b><i>x</b></i>"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "HTML"):
                normalize_telegram_html(text)


class RequiredAttributeTests(unittest.TestCase):
    def test_tags_missing_the_attribute_telegram_requires_are_refused(self) -> None:
        for text in ("<a>x</a>", "<span>x</span>", '<a href="">x</a>', "<tg-emoji>x</tg-emoji>"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "ویژگی"):
                normalize_telegram_html(text)

    def test_tags_with_the_attribute_are_kept(self) -> None:
        for text in ('<span class="tg-spoiler">x</span>', "<a href='https://t.me/x'>x</a>"):
            with self.subTest(text=text):
                self.assertEqual(normalize_telegram_html(text), text)


class DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'b.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add(BotUser(id=1, telegram_id=101, referral_code="u1", role="user"))
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self.tmp.cleanup()

    async def broadcasts(self, session) -> int:
        return int(await session.scalar(select(func.count()).select_from(BroadcastLog)))


class QueueTests(DbCase):
    async def test_stray_less_than_is_stored_escaped(self) -> None:
        async with self.Session() as session:
            log = await queue_broadcast(session, text="قیمت < ۱۰۰", audience="all", created_by="owner")
        self.assertEqual(log.text, "قیمت &lt; ۱۰۰")

    async def test_the_same_token_queues_one_broadcast(self) -> None:
        token = new_request_token()
        async with self.Session() as session:
            first = await queue_broadcast(session, text="سلام", audience="all", created_by="o", request_token=token)
            second = await queue_broadcast(session, text="سلام", audience="all", created_by="o", request_token=token)
            self.assertEqual(first.id, second.id)
            self.assertEqual(await self.broadcasts(session), 1)

    async def test_a_malformed_token_never_matches_another_broadcast(self) -> None:
        async with self.Session() as session:
            await queue_broadcast(session, text="a", audience="all", created_by="o", request_token="x' OR 1=1")
            await queue_broadcast(session, text="b", audience="all", created_by="o", request_token="x' OR 1=1")
            self.assertEqual(await self.broadcasts(session), 2)

    async def test_unbalanced_html_is_refused_before_queueing(self) -> None:
        async with self.Session() as session:
            with self.assertRaisesRegex(ValueError, "HTML"):
                await queue_broadcast(session, text="<b>سلام", audience="all", created_by="o")
            self.assertEqual(await self.broadcasts(session), 0)


def owner_message(text: str, *, entities=None) -> MagicMock:
    message = MagicMock()
    message.text = text
    message.entities = entities
    message.html_text = f"<b>{text}</b>" if entities else text
    message.answer = AsyncMock()
    return message


class PreviewTests(DbCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
        self.finish = patch.object(confirm, "finish_text_step", AsyncMock())
        self.finish_mock = self.finish.start()
        self.addCleanup(self.finish.stop)

    async def test_preview_sends_the_real_message_and_queues_nothing(self) -> None:
        message = owner_message("قیمت < ۱۰۰")
        async with self.Session() as session:
            await confirm.preview_broadcast(message, self.state, session, audience="all")
            self.assertEqual(await self.broadcasts(session), 0)
        message.answer.assert_awaited_once_with("قیمت &lt; ۱۰۰", parse_mode="HTML")
        self.assertEqual(await self.state.get_state(), confirm.BroadcastConfirmStates.confirm.state)
        self.assertIn("1 نفر", self.finish_mock.await_args.kwargs["text"])

    async def test_telegram_formatting_is_kept(self) -> None:
        message = owner_message("سلام", entities=[SimpleNamespace(type="bold")])
        async with self.Session() as session:
            await confirm.preview_broadcast(message, self.state, session, audience="all")
        self.assertEqual((await self.state.get_data())["bc_text"], "<b>سلام</b>")

    async def test_text_telegram_rejects_is_not_offered_for_sending(self) -> None:
        message = owner_message('<a href="bad">x</a>')
        message.answer = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message="can't parse entities"))
        async with self.Session() as session:
            await confirm.preview_broadcast(message, self.state, session, audience="all")
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(self.finish_mock.await_args.kwargs["text"], confirm.REJECTED_BY_TELEGRAM)

    async def test_unbalanced_tags_are_explained(self) -> None:
        async with self.Session() as session:
            await confirm.preview_broadcast(owner_message("<b>x"), self.state, session, audience="all")
        self.assertIn("HTML", self.finish_mock.await_args.kwargs["text"])


class StaleConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_second_tap_after_sending_keeps_the_success_message(self) -> None:
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
        callback = MagicMock()
        callback.answer = AsyncMock()
        callback.message = MagicMock()
        with patch.object(confirm, "safe_edit_text", AsyncMock()) as edit:
            await confirm.drop_broadcast(callback, MagicMock(), state)
        edit.assert_not_awaited()
        self.assertEqual(callback.answer.await_args.args[0], confirm.STALE_CONFIRM)

    async def test_cancel_while_pending_clears_the_state_and_edits_the_card(self) -> None:
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=101, user_id=101))
        await state.set_state(confirm.BroadcastConfirmStates.confirm)
        callback = MagicMock()
        callback.answer = AsyncMock()
        callback.message = MagicMock()
        with patch.object(confirm, "safe_edit_text", AsyncMock()) as edit, patch.object(
            confirm, "_back", AsyncMock(return_value=None)
        ):
            await confirm.drop_broadcast(callback, MagicMock(), state)
        self.assertIsNone(await state.get_state())
        self.assertEqual(edit.await_args.args[1], confirm.DROPPED)


if __name__ == "__main__":
    unittest.main()
