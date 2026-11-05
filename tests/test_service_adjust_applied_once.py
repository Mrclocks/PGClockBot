"""A staged day/GB change is applied once, even when confirm is tapped twice at the same time."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import admin as admin_handler
from app.bot.handlers import reseller_customer_services as shop_handler
from app.bot.service_adjust import NOTHING_STAGED, read_staged

USER_ID = 4
SERVICE_ID = 11
SHOP_KEY = f"rsvcadj:{USER_ID}:{SERVICE_ID}"
ADMIN_KEY = f"svcadj:{USER_ID}:{SERVICE_ID}"


def fsm() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


def callback() -> MagicMock:
    return MagicMock(answer=AsyncMock(), message=MagicMock())


async def slow_extend(*_args, **_kwargs) -> None:
    # Yield so a second confirm gets a chance to run while the first is "at the panel".
    await asyncio.sleep(0.01)


class ShopConfirmTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.target = shop_handler.ShopServiceTarget(
            owner_id=2, profile=MagicMock(), user_id=USER_ID, service_id=SERVICE_ID
        )
        self.extend = AsyncMock(side_effect=slow_extend)
        for name, value in (
            ("get_owned_service", AsyncMock(return_value=MagicMock())),
            ("shop_bot_panel_staff", AsyncMock(return_value={"role": "reseller"})),
            ("admin_extend_service", self.extend),
            ("service_snapshot", AsyncMock()),
            ("snapshot_telegram_lines", MagicMock(return_value="")),
            ("safe_edit_text", AsyncMock()),
        ):
            patcher = patch.object(shop_handler, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def confirm(self, state: FSMContext, cb: MagicMock) -> None:
        # The adjust handler reads the staged values and passes them in, as the bot does.
        staged = read_staged(await state.get_data(), SHOP_KEY)
        await shop_handler._adj_confirm(
            cb, state, AsyncMock(), MagicMock(), target=self.target, adj_key=SHOP_KEY,
            days=staged.days, gb=staged.gb,
        )

    async def test_two_confirms_at_once_extend_the_service_once(self) -> None:
        state = fsm()
        await state.update_data(**{SHOP_KEY: {"days": 5, "gb": 2}})
        first, second = callback(), callback()

        await asyncio.gather(self.confirm(state, first), self.confirm(state, second))

        self.extend.assert_awaited_once()
        self.assertEqual(self.extend.await_args.kwargs["extra_days"], 5)
        self.assertEqual(self.extend.await_args.kwargs["extra_gb"], 2.0)
        answers = [first.answer.await_args.args[0], second.answer.await_args.args[0]]
        self.assertTrue({shop_handler.IN_FLIGHT, "حداقل یک مقدار غیرصفر وارد کنید"} & set(answers))

    async def test_a_refused_change_stays_staged_for_a_retry(self) -> None:
        state = fsm()
        await state.update_data(**{SHOP_KEY: {"days": 5, "gb": 0}})
        self.extend.side_effect = ValueError("حجم نامعتبر است")

        await self.confirm(state, callback())

        self.assertEqual(read_staged(await state.get_data(), SHOP_KEY).days, 5)

    async def test_an_unknown_outcome_is_not_staged_again(self) -> None:
        state = fsm()
        await state.update_data(**{SHOP_KEY: {"days": 5, "gb": 0}})
        self.extend.side_effect = httpx.ReadTimeout("slow panel")
        cb = callback()

        await self.confirm(state, cb)

        self.assertTrue(read_staged(await state.get_data(), SHOP_KEY).is_empty)
        self.assertEqual(cb.answer.await_args.args[0], shop_handler.UNKNOWN_OUTCOME)

    async def test_an_unexpected_error_is_not_staged_again(self) -> None:
        state = fsm()
        await state.update_data(**{SHOP_KEY: {"days": 5, "gb": 0}})
        self.extend.side_effect = RuntimeError("connection dropped after the write")

        await self.confirm(state, callback())

        self.assertTrue(read_staged(await state.get_data(), SHOP_KEY).is_empty)


class PlatformAdminConfirmTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.extend = AsyncMock(side_effect=slow_extend)
        for name, value in (
            ("get_owned_service", AsyncMock(return_value=MagicMock())),
            ("admin_extend_service", self.extend),
            ("service_snapshot", AsyncMock()),
            ("snapshot_telegram_lines", MagicMock(return_value="")),
            ("safe_edit_text", AsyncMock()),
        ):
            patcher = patch.object(admin_handler, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def confirm(self, state: FSMContext, cb: MagicMock) -> None:
        await admin_handler._adm_confirm_service_adjust(
            cb, state, AsyncMock(), user_id=USER_ID, service_id=SERVICE_ID, adj_key=ADMIN_KEY
        )

    async def test_two_confirms_at_once_extend_the_service_once(self) -> None:
        state = fsm()
        await state.update_data(**{ADMIN_KEY: {"days": 3, "gb": 0}})

        await asyncio.gather(self.confirm(state, callback()), self.confirm(state, callback()))

        self.extend.assert_awaited_once()

    async def test_a_refused_change_stays_staged_for_a_retry(self) -> None:
        state = fsm()
        await state.update_data(**{ADMIN_KEY: {"days": 3, "gb": 0}})
        self.extend.side_effect = ValueError("تعداد روز نامعتبر است")

        await self.confirm(state, callback())

        self.assertEqual(read_staged(await state.get_data(), ADMIN_KEY).days, 3)

    async def test_confirm_with_nothing_staged_does_not_call_the_panel(self) -> None:
        cb = callback()

        await self.confirm(fsm(), cb)

        self.extend.assert_not_awaited()
        self.assertEqual(cb.answer.await_args.args[0], NOTHING_STAGED)


if __name__ == "__main__":
    unittest.main()
