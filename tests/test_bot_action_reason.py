"""Telegram admin flows ask for a reason first; a reason authorizes exactly one callback, once."""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import action_reason as gate
from app.db.models import BotUser
from app.services.action_reasons import bot_actor
from app.services.staff_audit import ACTOR_PLATFORM_OWNER, ACTOR_SHOP_OWNER, ACTOR_STAFF


def fsm() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


def callback(data: str) -> MagicMock:
    return MagicMock(data=data, answer=AsyncMock(), message=MagicMock())


def typed(text: str) -> MagicMock:
    return MagicMock(text=text, answer=AsyncMock())


class ReasonGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        patcher = patch.object(gate, "safe_edit_text", AsyncMock())
        self.edit = patcher.start()
        self.addCleanup(patcher.stop)

    async def collect(self, state: FSMContext, cb: MagicMock, text: str = "درخواست مشتری") -> MagicMock:
        await gate.ask_reason(cb, state, label="علت حذف")
        message = typed(text)
        await gate.reason_typed(message, state)
        return message

    async def test_without_a_reason_the_prompt_is_shown_and_nothing_is_authorized(self) -> None:
        state, cb = fsm(), callback("adm:users:svcdel:1:2")
        self.assertIsNone(await gate.take_reason(state, cb))
        await gate.ask_reason(cb, state, label="علت حذف")
        self.assertEqual(await state.get_state(), gate.ActionReasonStates.reason.state)
        self.assertIsNone(await gate.take_reason(state, cb))

    async def test_a_typed_reason_authorizes_the_same_callback_once(self) -> None:
        state, cb = fsm(), callback("adm:users:svcdel:1:2")
        message = await self.collect(state, cb)
        button = message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.callback_data, cb.data)
        self.assertEqual(await gate.take_reason(state, cb), "درخواست مشتری")
        self.assertIsNone(await gate.take_reason(state, cb), "a reason is consumed by the action it authorized")

    async def test_a_reason_for_one_service_cannot_authorize_another(self) -> None:
        state = fsm()
        await self.collect(state, callback("adm:users:svcdel:1:2"))
        self.assertIsNone(await gate.take_reason(state, callback("adm:users:svcdel:1:3")))

    async def test_a_too_short_reason_is_refused_and_the_prompt_stays_open(self) -> None:
        state, cb = fsm(), callback("adm:users:svcdel:1:2")
        message = await self.collect(state, cb, text="ab")
        self.assertEqual(await state.get_state(), gate.ActionReasonStates.reason.state)
        self.assertIsNone(await gate.take_reason(state, cb))
        self.assertIn("الزامی", message.answer.await_args.args[0])

    async def test_cancel_clears_the_pending_reason(self) -> None:
        state, cb = fsm(), callback("adm:users:svcdel:1:2")
        await self.collect(state, cb)
        await gate.reason_cancelled(callback(gate.CANCEL_CALLBACK), state)
        self.assertIsNone(await gate.take_reason(state, cb))
        self.assertIsNone(await state.get_state())

    async def test_without_a_state_the_action_is_never_authorized(self) -> None:
        cb = callback("adm:users:svcdel:1:2")
        self.assertIsNone(await gate.take_reason(None, cb))
        await gate.ask_reason(cb, None, label="علت حذف")
        cb.answer.assert_awaited_once()


class BotActorTests(unittest.TestCase):
    def test_the_actor_kind_follows_the_shop(self) -> None:
        owner = BotUser(id=1, telegram_id=11, username="boss")
        self.assertEqual(bot_actor(owner, shop_id=None).kind, ACTOR_PLATFORM_OWNER)
        self.assertEqual(bot_actor(owner, shop_id=1).kind, ACTOR_SHOP_OWNER)
        self.assertEqual(bot_actor(owner, shop_id=2).kind, ACTOR_STAFF)
        self.assertEqual(bot_actor(owner, shop_id=2).shop_id, 2)


if __name__ == "__main__":
    unittest.main()
