"""ask_text / finish_text_step / nv:cancel contract (customer typed input)."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import app.bot.keyboards  # noqa: F401


class AskTextHelperTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Ensure cancel codes from handlers are registered.
        import app.bot.handlers.wallet  # noqa: F401
        import app.bot.handlers.shop  # noqa: F401
        import app.bot.handlers.support  # noqa: F401

    async def test_ask_text_stores_prompt_and_inline_cancel(self):
        from app.bot.nav_input import PROMPT_MSG_ID, ask_text
        from app.bot.handlers.wallet import WalletStates

        message = AsyncMock()
        sent = MagicMock()
        sent.message_id = 77
        sent.chat = MagicMock(id=5)
        message.answer = AsyncMock(return_value=sent)
        state = AsyncMock()
        state.set_state = AsyncMock()
        state.update_data = AsyncMock()

        out = await ask_text(
            message,
            state,
            prompt="مبلغ را وارد کنید",
            cancel_code="w_amt",
            fsm_state=WalletStates.topup_amount,
        )

        self.assertIs(out, sent)
        state.set_state.assert_awaited()
        markup = message.answer.await_args.kwargs.get("reply_markup")
        flat = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("nv:cancel:w_amt", flat)
        # Prompt id stored for finish_text_step
        kwargs = state.update_data.await_args.kwargs
        self.assertEqual(kwargs.get(PROMPT_MSG_ID), 77)

    async def test_finish_text_step_edits_prompt(self):
        from app.bot.nav_input import PROMPT_CHAT_ID, PROMPT_MSG_ID, finish_text_step

        message = AsyncMock()
        message.chat = MagicMock(id=5)
        message.message_id = 99
        message.bot = AsyncMock()
        message.bot.edit_message_text = AsyncMock()
        message.answer = AsyncMock()
        state = AsyncMock()
        state.get_data = AsyncMock(
            return_value={PROMPT_MSG_ID: 77, PROMPT_CHAT_ID: 5}
        )
        state.update_data = AsyncMock()

        await finish_text_step(
            message, state, text="روش واریز", inline=MagicMock()
        )

        message.bot.edit_message_text.assert_awaited()
        message.answer.assert_not_awaited()

    async def test_forged_cancel_code_refused(self):
        from app.bot.nav_input import nv_cancel_input

        callback = AsyncMock()
        callback.data = "nv:cancel:not_a_real_code"
        callback.answer = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()

        await nv_cancel_input(callback, session, db_user, state)
        callback.answer.assert_awaited()
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))

    async def test_registered_codes_include_customer_flows(self):
        from app.bot.nav_input import CANCEL_REGISTRY

        for code in ("w_amt", "w_qty", "s_subj", "s_body"):
            self.assertIn(code, CANCEL_REGISTRY)


if __name__ == "__main__":
    unittest.main()
