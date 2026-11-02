"""Bot admin user delete must ask for reason and never attach ReplyKeyboard to edit_text."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADMIN = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
KB = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")


class BotUserDeleteReasonTests(unittest.TestCase):
    def test_delete_user_reason_state_exists(self):
        self.assertIn("delete_user_reason = State()", ADMIN)

    def test_confirm_callback_asks_for_reason_not_deletes(self):
        # Confirm button handler should enter FSM, not call delete_bot_user.
        start = ADMIN.find("async def adm_users_delete_ask_reason")
        self.assertGreater(start, 0)
        block = ADMIN[start : ADMIN.find("@router.message(AdminStates.delete_user_reason)", start)]
        self.assertIn("AdminStates.delete_user_reason", block)
        self.assertIn("علت حذف کامل کاربر", block)
        self.assertNotIn("delete_bot_user", block)
        self.assertNotIn('event="user_delete"', block)

    def test_reason_handler_notifies_then_deletes(self):
        start = ADMIN.find("async def adm_users_delete_reason")
        self.assertGreater(start, 0)
        block = ADMIN[start : ADMIN.find("@router.callback_query(F.data.startswith(\"adm:users:unres:\"))", start)]
        self.assertIn("notify_account_edit", block)
        self.assertIn('event="user_delete"', block)
        self.assertIn("reason=reason", block)
        self.assertIn("delete_bot_user", block)
        self.assertLess(block.find("notify_account_edit"), block.find("delete_bot_user"))
        # The outcome replaces the prompt; no reply keyboard is sent with it.
        self.assertNotIn("admin_users_reply_keyboard()", block)
        self.assertIn("message.answer(", block)

    def test_no_legacy_hardcoded_bot_delete_reason(self):
        self.assertNotIn('reason="حذف از پنل ربات ادمین"', ADMIN)

    def test_confirm_button_label_mentions_reason(self):
        self.assertIn("نوشتن علت حذف", KB)
        self.assertIn('callback_data=f"adm:users:del:{user_id}"', KB)

    def test_confirm_card_copy_mentions_reason_prompt(self):
        self.assertIn("بعد از تأیید، علت حذف پرسیده می‌شود", ADMIN)


class PanelCommitReasonImeTests(unittest.TestCase):
    def test_commit_reason_reads_before_blur(self):
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        fn = js.split("function commitReasonInput(input){", 1)[1].split("\n      }", 1)[0]
        self.assertIn("Read BEFORE blur", fn)
        self.assertIn("before", fn)
        self.assertIn("after", fn)

    def test_confirm_success_refuses_empty_reason_override(self):
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        # Both confirm success paths must refuse empty reason when required.
        self.assertGreaterEqual(js.count("if (!reasonText) return;"), 2)
        self.assertIn("overrides.confirm_reason = reasonText;", js)


if __name__ == "__main__":
    unittest.main()
