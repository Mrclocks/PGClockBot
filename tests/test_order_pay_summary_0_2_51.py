"""Fix 1: payment panel must keep order summary (id + amount) in one edit."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def _bot_callback_message() -> MagicMock:
    message = MagicMock()
    message.from_user = MagicMock(is_bot=True)
    message.message_id = 42
    message.chat = MagicMock(id=7)
    message.edit_text = AsyncMock()
    message.answer = AsyncMock()
    message.bot = MagicMock()
    return message


class PresentOrderPaySummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_and_prompt_in_one_edit(self):
        from app.bot.menu_nav import present_order_pay

        message = _bot_callback_message()
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={})
        state.update_data = AsyncMock()
        pay_kb = MagicMock(name="PAY_KB")

        with (
            patch(
                "app.bot.menu_nav.get_all_settings",
                AsyncMock(return_value={}),
            ),
            patch(
                "app.bot.menu_nav.set_nav_level",
                AsyncMock(),
            ),
            patch(
                "app.bot.keyboards.pay_methods",
                return_value=pay_kb,
            ),
            patch(
                "app.bot.nav_inline.present_inline_only",
                AsyncMock(return_value=message),
            ) as present,
        ):
            await present_order_pay(
                message,
                session,
                db_user,
                99,
                state=state,
                summary="🛒 سفارش #۹۹\nمبلغ: <b>۵۰٬۰۰۰ تومان</b>",
                is_reseller_bot=False,
                reseller_owner_id=None,
            )

        present.assert_awaited_once()
        kwargs = present.await_args.kwargs
        body = kwargs["text"]
        self.assertIn("#۹۹", body)
        self.assertIn("۵۰٬۰۰۰", body)
        self.assertIn("روش پرداخت", body)
        self.assertIs(kwargs["inline"], pay_kb)
        message.answer.assert_not_awaited()

    async def test_keyboard_payin_phrase_not_stripped_to_bare_prompt(self):
        from app.bot.menu_nav import present_order_pay

        message = _bot_callback_message()
        session = AsyncMock()
        db_user = MagicMock()

        with (
            patch(
                "app.bot.menu_nav.get_all_settings",
                AsyncMock(return_value={}),
            ),
            patch("app.bot.keyboards.pay_methods", return_value=MagicMock()),
            patch(
                "app.bot.nav_inline.present_inline_only",
                AsyncMock(return_value=message),
            ) as present,
        ):
            await present_order_pay(
                message,
                session,
                db_user,
                1,
                summary="سفارش #1\nمبلغ: 1000",
                text="💳 روش پرداخت را از کیبورد پایین انتخاب کنید:",
                is_reseller_bot=False,
                reseller_owner_id=None,
            )

        body = present.await_args.kwargs["text"]
        self.assertIn("سفارش #1", body)
        self.assertIn("مبلغ", body)
        # Must not collapse to only the bare prompt.
        self.assertNotEqual(body.strip(), "روش پرداخت را انتخاب کنید:")


class ShowOrderPayNoDoubleEditTests(unittest.IsolatedAsyncioTestCase):
    async def test_show_order_pay_does_not_pre_edit(self):
        from app.bot.handlers.shop import _show_order_pay

        message = _bot_callback_message()
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        summary = "پلن تست\nسفارش #5\nمبلغ: <b>۱۰٬۰۰۰ تومان</b>"

        with patch(
            "app.bot.menu_nav.present_order_pay",
            AsyncMock(),
        ) as pay:
            await _show_order_pay(
                message,
                session,
                db_user,
                5,
                state,
                summary,
                is_reseller_bot=False,
                reseller_owner_id=None,
            )

        pay.assert_awaited_once()
        self.assertEqual(pay.await_args.kwargs.get("summary"), summary)
        message.edit_text.assert_not_awaited()
        # No pre-answer of the summary before present_order_pay.
        message.answer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
