"""Reply keyboard must survive FSM text input (no tip-delete-only restore)."""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

    def test_wallet_credit_and_block_restore_users_kb(self):
        src = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        credit = src.split("async def adm_users_wallet_credit_save(", 1)[1].split(
            "\n@router.", 1
        )[0]
        self.assertIn("reply_markup=kb.admin_users_reply_keyboard()", credit)
        block = src.split("async def adm_users_block_reason(", 1)[1].split(
            "\n@router.", 1
        )[0]
        self.assertIn("reply_markup=kb.admin_users_reply_keyboard()", block)

    def test_adjust_gb_restores_users_kb_before_inline(self):
        src = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        fn = src.split("async def adm_svc_adjust_gb_entered(", 1)[1].split(
            "\n@router.", 1
        )[0]
        self.assertIn(
            'await message.answer("لغو شد.", reply_markup=kb.admin_users_reply_keyboard())',
            fn,
        )
        self.assertIn(
            'await message.answer("👥 کاربران", reply_markup=kb.admin_users_reply_keyboard())',
            fn,
        )
        self.assertLess(
            fn.index("👥 کاربران"),
            fn.index("admin_user_service_adjust_keyboard"),
        )


class SourceContractTests(unittest.TestCase):
    def test_seed_default_not_ephemeral(self):
        from app.bot.tg_utils import seed_reply_keyboard

        sig = inspect.signature(seed_reply_keyboard)
        self.assertEqual(sig.parameters["ephemeral"].default, False)

    def test_start_seed_uses_ephemeral_polish(self):
        src = (ROOT / "app/bot/handlers/start.py").read_text(encoding="utf-8")
        self.assertIn("ephemeral=True", src)


if __name__ == "__main__":
    unittest.main()
