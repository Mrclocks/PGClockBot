"""After a receipt photo, only the confirmation is sent — not a fresh welcome screen."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.menu_nav import acknowledge_receipt


class ReceiptAckNoWelcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_acknowledge_receipt_answers_text_without_render_home(self) -> None:
        message = MagicMock()
        message.answer = AsyncMock()
        session = AsyncMock()
        db_user = SimpleNamespace(id=1)
        state = AsyncMock()
        kb = object()
        with (
            patch("app.bot.menu_nav.clear_checkout_nav", AsyncMock()) as clear_nav,
            patch(
                "app.bot.menu_nav.buyer_main_reply_keyboard",
                AsyncMock(return_value=(kb, {})),
            ) as build_kb,
            patch("app.bot.handlers.start.render_home", AsyncMock()) as home,
        ):
            await acknowledge_receipt(
                message,
                session,
                db_user,
                text="✅ رسید دریافت شد\nرسید شما ثبت شد.",
                state=state,
                is_reseller_bot=False,
                reseller_owner_id=None,
            )
        clear_nav.assert_awaited_once()
        build_kb.assert_awaited_once()
        home.assert_not_awaited()
        message.answer.assert_awaited_once()
        self.assertEqual(
            message.answer.await_args.args[0],
            "✅ رسید دریافت شد\nرسید شما ثبت شد.",
        )
        self.assertIs(message.answer.await_args.kwargs.get("reply_markup"), kb)


if __name__ == "__main__":
    unittest.main()
