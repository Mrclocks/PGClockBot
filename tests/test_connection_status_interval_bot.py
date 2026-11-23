"""The connection test's check interval from the bot: the page shows it, the picker marks the saved one, a choice
is saved through the same service as the web panel, and nothing else (another value, a shop bot) can save one."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy import select

from connection_monitor_support import MonitorDatabaseCase, monitor_access

from app.bot.connection_status_admin_ui import parse_monitor_callback
from app.bot.connection_status_ui import admin_page_keyboard, admin_page_text, interval_keyboard
from app.bot.handlers import connection_status_admin as admin
from app.db.models import BotUser, Setting
from app.services.connection_monitor_access import MonitorAccess
from app.services.connection_monitor_timing import INTERVAL_SETTING_KEY
from app.services.connection_monitor_views import monitor_admin_page
from app.services.users import DEFAULT_SETTINGS


class IntervalPickerTests(MonitorDatabaseCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.message = Message(message_id=100, date=datetime.now(timezone.utc), chat=Chat(id=101, type="private"),
                               from_user=User(id=101, is_bot=False, first_name="Owner"), text="x")
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=101, user_id=101))
        self.user = BotUser(id=1, telegram_id=101, referral_code="owner", role="admin")
        for target, attr in ((CallbackQuery, "answer"), (Message, "answer")):
            patcher = patch.object(target, attr, new_callable=AsyncMock)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        await super().asyncTearDown()

    async def press(self, data: str, access: MonitorAccess) -> AsyncMock:
        callback = CallbackQuery(id="cb", from_user=self.message.from_user, chat_instance="c", data=data, message=self.message)
        with patch.object(admin, "bot_monitor_access", AsyncMock(return_value=access)), \
                patch.object(admin, "safe_edit_text", new_callable=AsyncMock) as render, \
                patch("app.bot.connection_status_admin_ui.safe_edit_text", render):
            async with self.Session() as session:
                await admin.monitor_admin_callback(callback, session, self.user, self.state)
        return render

    async def saved(self) -> str | None:
        async with self.Session() as session:
            return await session.scalar(select(Setting.value).where(Setting.key == INTERVAL_SETTING_KEY))

    async def test_the_page_shows_the_interval_and_offers_the_picker(self) -> None:
        async with self.Session() as session:
            page = await monitor_admin_page(session, self.access, now=self.now)
        self.assertIn("فاصلهٔ بررسی: هر ۵ دقیقه", admin_page_text(page, DEFAULT_SETTINGS, interval_seconds=300))
        callbacks = [b.callback_data for row in admin_page_keyboard(page, DEFAULT_SETTINGS).inline_keyboard for b in row]
        self.assertIn("cma:interval:0", callbacks)

    async def test_the_picker_marks_the_saved_interval(self) -> None:
        rows = interval_keyboard(120, DEFAULT_SETTINGS).inline_keyboard
        labels = {b.callback_data: b.text for row in rows for b in row}
        self.assertEqual(labels["cma:every:120"], "✅ هر ۲ دقیقه")
        self.assertEqual(labels["cma:every:60"], "هر ۱ دقیقه")
        render = await self.press("cma:interval:0", self.access)
        self.assertIn("فاصلهٔ بررسی", render.call_args.args[1])

    async def test_the_owner_saves_a_choice_and_returns_to_the_page(self) -> None:
        render = await self.press("cma:every:600", self.access)
        self.assertEqual(await self.saved(), "600")
        self.assertIn("فاصلهٔ بررسی: هر ۱۰ دقیقه", render.call_args.args[1])

    async def test_a_value_that_is_not_offered_is_refused(self) -> None:
        for raw in ("cma:every:61", "cma:every:0", "cma:every:86400"):
            with self.subTest(raw=raw):
                await self.press(raw, self.access)
                self.assertIsNone(await self.saved())
        with self.assertRaises(ValueError):
            parse_monitor_callback("cma:every:-60")

    async def test_a_shop_bot_cannot_change_the_interval(self) -> None:
        shop_bot = MonitorAccess(monitor_access().authz, False)
        await self.press("cma:every:600", shop_bot)
        self.assertIsNone(await self.saved())
