"""Broadcast history in the bot: owner-only, escaped text, one-time stop."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import broadcast_history as history
from app.db.models import Base, BroadcastLog
from app.services import bot_feedback_texts as feedback


def callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class BroadcastHistoryBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BroadcastLog(id=1, audience="all", text="<b>hello</b>", total=10, ok_count=4, status="running"),
            BroadcastLog(id=2, audience="all", text="old", total=3, ok_count=3, status="done"),
        ])
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def press(self, data: str, *, owner: bool = True):
        handler = {"bch:l": history.show_list, "v": history.show_card, "c": history.confirm_cancel, "x": history.run_cancel}[
            data if data == "bch:l" else data.split(":")[1]
        ]
        callback = AsyncMock()
        callback.data = data
        callback.message = AsyncMock()
        with patch.object(history, "require_bot_owner", AsyncMock(return_value=owner)), \
                patch.object(history, "safe_edit_text", AsyncMock()) as edit:
            await handler(callback, session=self.session, db_user=SimpleNamespace(id=9))
        return callback, edit

    async def test_non_owner_is_denied_everywhere_and_nothing_is_stopped(self) -> None:
        for data in ("bch:l", "bch:v:1", "bch:c:1", "bch:x:1"):
            callback, edit = await self.press(data, owner=False)
            callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
            edit.assert_not_awaited()
        log = await self.session.get(BroadcastLog, 1)
        await self.session.refresh(log)
        self.assertEqual(log.status, "running")

    async def test_list_card_and_escaping(self) -> None:
        _, edit = await self.press("bch:l")
        self.assertIn("bch:v:1", callbacks(edit.await_args.kwargs["reply_markup"]))
        _, edit = await self.press("bch:v:1")
        self.assertIn("&lt;b&gt;hello&lt;/b&gt;", edit.await_args.args[1])
        self.assertIn("bch:c:1", callbacks(edit.await_args.kwargs["reply_markup"]))

    async def test_finished_broadcast_offers_no_stop(self) -> None:
        _, edit = await self.press("bch:v:2")
        self.assertNotIn("bch:c:2", callbacks(edit.await_args.kwargs["reply_markup"]))
        callback, _ = await self.press("bch:c:2")
        self.assertIn("فعال نیست", callback.answer.await_args.args[0])

    async def test_stop_needs_confirmation_and_works_once(self) -> None:
        await self.press("bch:c:1")
        log = await self.session.get(BroadcastLog, 1)
        await self.session.refresh(log)
        self.assertEqual(log.status, "running")
        await self.press("bch:x:1")
        await self.session.refresh(log)
        self.assertEqual(log.status, "cancelled")
        callback, _ = await self.press("bch:x:1")
        self.assertIn("فعال نیست", callback.answer.await_args.args[0])

    async def test_unknown_id_is_not_found(self) -> None:
        callback, edit = await self.press("bch:v:99")
        callback.answer.assert_awaited_once_with(feedback.NOT_FOUND, show_alert=True)
        edit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
