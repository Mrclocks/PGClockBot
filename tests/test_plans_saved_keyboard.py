"""Saving a plan step never swaps the menu keyboard for a plans submenu."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import ReplyKeyboardMarkup
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import admin_plans
from app.db.models import Base, BotUser

MAIN = ReplyKeyboardMarkup(keyboard=[])


class SavedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'p.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add(BotUser(id=1, telegram_id=7001, referral_code="o1", role="admin"))
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def saved(self, telegram_id: int) -> AsyncMock:
        message = SimpleNamespace(from_user=SimpleNamespace(id=telegram_id), answer=AsyncMock())
        with patch("app.bot.menu_nav.build_main_reply_keyboard", AsyncMock(return_value=(MAIN, {}, "admin"))):
            await admin_plans._answer_plans_saved(message, self.session, "✅")
        return message.answer

    async def test_a_known_account_gets_the_main_keyboard(self) -> None:
        answer = await self.saved(7001)
        self.assertIs(answer.await_args.kwargs["reply_markup"], MAIN)

    async def test_an_unknown_account_gets_no_keyboard_rather_than_a_submenu(self) -> None:
        answer = await self.saved(9999)
        self.assertNotIn("reply_markup", answer.await_args.kwargs)


if __name__ == "__main__":
    unittest.main()
