"""Reply keyboard must survive FSM text input (no tip-delete-only restore)."""

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


class AdminSearchRestoresUsersKeyboardTests(unittest.TestCase):
    def test_search_success_attaches_users_reply_kb(self):
        src = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        fn = src.split("async def adm_users_search(", 1)[1].split("\nasync def ", 1)[0]
        self.assertIn("admin_users_reply_keyboard()", fn)
        self.assertIn("نتیجه جستجو", fn)
        self.assertNotIn("seed_persistent_reply_kb", fn)
        self.assertLess(fn.index("نتیجه جستجو"), fn.index("_render_user_card"))

class AdminFsmFinishKeepsReplyKeyboardTests(unittest.IsolatedAsyncioTestCase):
    """Finishing an admin FSM text step re-attaches the lasting reply keyboard."""

    def _handler_context(self, text: str):
        message = MagicMock()
        message.text = text
        message.answer = AsyncMock()
        state = AsyncMock()
        db_user = MagicMock()
        db_user.role = "admin"
        session = AsyncMock()
        return message, state, db_user, session

    def _patches(self, stack, lasting_markup):
        from unittest.mock import patch

        for target, replacement in (
            ("app.bot.auth.require_bot_owner", AsyncMock(return_value=True)),
            ("app.bot.handlers.admin._is_admin", MagicMock(return_value=True)),
            (
                "app.bot.handlers.admin._staff_reply",
                AsyncMock(return_value=lasting_markup),
            ),
            ("app.bot.handlers.admin._render_user_card", AsyncMock()),
        ):
            stack.enter_context(patch(target, replacement))

    async def test_wallet_credit_save_attaches_lasting_reply_keyboard(self):
        from contextlib import ExitStack
        from unittest.mock import patch

        from app.bot.handlers.admin import adm_users_wallet_credit_save

        lasting = object()
        message, state, db_user, session = self._handler_context("5000")
        state.get_data = AsyncMock(
            return_value={"admin_credit_user_id": 5, "admin_wallet_mode": "credit"}
        )
        user = MagicMock()
        user.wallet_balance = 5000
        with ExitStack() as stack:
            self._patches(stack, lasting)
            stack.enter_context(
                patch(
                    "app.bot.handlers.admin._platform_shop_user",
                    AsyncMock(return_value=(user, None)),
                )
            )
            stack.enter_context(
                patch("app.bot.handlers.admin.load_reseller_profile", AsyncMock(return_value=None))
            )
            adjust = stack.enter_context(
                patch(
                    "app.services.bot_user_admin.admin_adjust_user_wallet",
                    AsyncMock(),
                )
            )
            await adm_users_wallet_credit_save(
                message, state, session=session, db_user=db_user
            )

        adjust.assert_awaited_once()
        message.answer.assert_awaited_once()
        self.assertIs(message.answer.await_args.kwargs["reply_markup"], lasting)

    async def test_block_reason_attaches_lasting_reply_keyboard(self):
        from contextlib import ExitStack
        from unittest.mock import patch

        from app.bot.handlers.admin import adm_users_block_reason

        lasting = object()
        message, state, db_user, session = self._handler_context("spam account")
        state.get_data = AsyncMock(return_value={"block_user_id": 5})
        user = MagicMock(id=5)
        with ExitStack() as stack:
            self._patches(stack, lasting)
            synchronize_customer = stack.enter_context(patch("app.bot.handlers.admin.set_legacy_customer_blocked", AsyncMock()))
            stack.enter_context(
                patch(
                    "app.bot.handlers.admin._platform_shop_user",
                    AsyncMock(return_value=(user, None)),
                )
            )
            stack.enter_context(
                patch("app.services.users.is_protected_admin", return_value=False)
            )
            stack.enter_context(
                patch("app.services.notifications.notify_account_edit", AsyncMock())
            )
            await adm_users_block_reason(
                message, state, session=session, db_user=db_user
            )

        self.assertTrue(user.is_blocked)
        synchronize_customer.assert_awaited_once_with(session, user, blocked=True)
        message.answer.assert_awaited_once()
        self.assertIs(message.answer.await_args.kwargs["reply_markup"], lasting)

    async def test_adjust_gb_restores_reply_kb_before_inline_adjuster(self):
        from contextlib import ExitStack

        from app.bot.handlers.admin import adm_svc_adjust_gb_entered

        lasting = object()
        message, state, db_user, session = self._handler_context("2")
        state.get_data = AsyncMock(return_value={"svcadj_uid": 5, "svcadj_sid": 7})
        with ExitStack() as stack:
            self._patches(stack, lasting)
            await adm_svc_adjust_gb_entered(
                message, state, session=session, db_user=db_user
            )

        first, second = message.answer.await_args_list
        self.assertIs(first.kwargs["reply_markup"], lasting)
        inline = second.kwargs["reply_markup"]
        self.assertIsInstance(inline, InlineKeyboardMarkup)
        self.assertIsNot(inline, lasting)


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
