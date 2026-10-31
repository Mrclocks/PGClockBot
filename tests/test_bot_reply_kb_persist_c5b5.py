"""The menu keyboard survives text steps, and a finished step never adds a message."""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import InlineKeyboardMarkup

ROOT = Path(__file__).resolve().parents[1]


class SeedReplyKeyboardSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_seed_keeps_tip_by_default(self):
        from app.bot.tg_utils import seed_reply_keyboard

        tip_msg = MagicMock()
        tip_msg.delete = AsyncMock()
        message = MagicMock()
        message.answer = AsyncMock(return_value=tip_msg)

        await seed_reply_keyboard(message, MagicMock(), tip="🏠")
        message.answer.assert_awaited_once()
        tip_msg.delete.assert_not_awaited()

    async def test_seed_ephemeral_deletes_tip(self):
        from app.bot.tg_utils import seed_reply_keyboard

        tip_msg = MagicMock()
        tip_msg.delete = AsyncMock()
        message = MagicMock()
        message.answer = AsyncMock(return_value=tip_msg)

        await seed_reply_keyboard(message, MagicMock(), tip="·", ephemeral=True)
        tip_msg.delete.assert_awaited_once()

    async def test_attach_reply_keyboard_never_deletes(self):
        from app.bot.tg_utils import attach_reply_keyboard

        sent = MagicMock()
        sent.delete = AsyncMock()
        message = MagicMock()
        message.answer = AsyncMock(return_value=sent)
        out = await attach_reply_keyboard(message, MagicMock(), text="⌨️ منو")
        self.assertIs(out, sent)
        message.answer.assert_awaited_once()
        sent.delete.assert_not_awaited()

    async def test_seed_persistent_uses_lasting_chrome(self):
        from app.bot import tg_utils as tu

        message = MagicMock()
        sent = MagicMock()
        sent.delete = AsyncMock()
        message.answer = AsyncMock(return_value=sent)
        await tu.seed_persistent_reply_kb(message)
        message.answer.assert_awaited_once()
        self.assertIn("reply_markup", message.answer.await_args.kwargs)
        sent.delete.assert_not_awaited()


class AdminFsmFinishShowsOutcomeInPlaceTests(unittest.IsolatedAsyncioTestCase):
    """Finishing an admin text step turns the prompt into the outcome; nothing else is sent."""

    CARD = InlineKeyboardMarkup(inline_keyboard=[])

    def _handler_context(self, text: str):
        message = MagicMock()
        message.text = text
        message.answer = AsyncMock()
        state = AsyncMock()
        db_user = MagicMock()
        db_user.role = "admin"
        session = AsyncMock()
        return message, state, db_user, session

    def _patches(self, stack):
        from unittest.mock import patch

        for target, replacement in (
            ("app.bot.auth.require_bot_owner", AsyncMock(return_value=True)),
            ("app.bot.handlers.admin._is_admin", MagicMock(return_value=True)),
            ("app.bot.handlers.admin._user_card", AsyncMock(return_value=("card", self.CARD))),
        ):
            stack.enter_context(patch(target, replacement))
        return stack.enter_context(patch("app.bot.handlers.admin.finish_text_step", AsyncMock()))

    def assert_one_in_place(self, message, finish, status: str) -> None:
        message.answer.assert_not_awaited()
        finish.assert_awaited_once()
        self.assertIn(status, finish.await_args.kwargs["text"])
        self.assertIsInstance(finish.await_args.kwargs["inline"], InlineKeyboardMarkup)

    async def test_wallet_credit_shows_the_outcome_on_the_card(self):
        from contextlib import ExitStack
        from unittest.mock import patch

        from app.bot.handlers.admin import adm_users_wallet_credit_save

        message, state, db_user, session = self._handler_context("5000")
        state.get_data = AsyncMock(return_value={"admin_credit_user_id": 5, "admin_wallet_mode": "credit"})
        user = MagicMock()
        user.wallet_balance = 5000
        with ExitStack() as stack:
            finish = self._patches(stack)
            stack.enter_context(patch("app.bot.handlers.admin._platform_shop_user", AsyncMock(return_value=(user, None))))
            stack.enter_context(patch("app.bot.handlers.admin.load_reseller_profile", AsyncMock(return_value=None)))
            adjust = stack.enter_context(patch("app.services.bot_user_admin.admin_adjust_user_wallet", AsyncMock()))
            await adm_users_wallet_credit_save(message, state, session=session, db_user=db_user)
        adjust.assert_awaited_once()
        self.assert_one_in_place(message, finish, "کیف پول شارژ شد")

    async def test_block_reason_shows_the_outcome_on_the_card(self):
        from contextlib import ExitStack
        from unittest.mock import patch

        from app.bot.handlers.admin import adm_users_block_reason

        message, state, db_user, session = self._handler_context("spam account")
        state.get_data = AsyncMock(return_value={"block_user_id": 5})
        user = MagicMock()
        with ExitStack() as stack:
            finish = self._patches(stack)
            stack.enter_context(patch("app.bot.handlers.admin._platform_shop_user", AsyncMock(return_value=(user, None))))
            stack.enter_context(patch("app.services.users.is_protected_admin", return_value=False))
            stack.enter_context(patch("app.services.notifications.notify_account_edit", AsyncMock()))
            await adm_users_block_reason(message, state, session=session, db_user=db_user)
        self.assertTrue(user.is_blocked)
        self.assert_one_in_place(message, finish, "کاربر مسدود شد")

    async def test_adjust_gb_shows_the_adjuster_in_place(self):
        from contextlib import ExitStack

        from app.bot.handlers.admin import adm_svc_adjust_gb_entered

        message, state, db_user, session = self._handler_context("2")
        state.get_data = AsyncMock(return_value={"svcadj_uid": 5, "svcadj_sid": 7})
        with ExitStack() as stack:
            finish = self._patches(stack)
            await adm_svc_adjust_gb_entered(message, state, session=session, db_user=db_user)
        self.assert_one_in_place(message, finish, "تغییر مانده سرویس #7")


class SourceContractTests(unittest.TestCase):
    def test_seed_default_not_ephemeral(self):
        from app.bot.tg_utils import seed_reply_keyboard

        sig = inspect.signature(seed_reply_keyboard)
        self.assertEqual(sig.parameters["ephemeral"].default, False)

    def test_start_seed_uses_lasting_attach(self):
        src = (ROOT / "app/bot/handlers/start.py").read_text(encoding="utf-8")
        self.assertIn("attach_reply_keyboard", src)
        self.assertNotIn("ephemeral=True", src)
        # Home no longer dual-sends via send_reply_keyboard_last (mini on welcome).
        self.assertNotIn("send_reply_keyboard_last", src)


if __name__ == "__main__":
    unittest.main()
