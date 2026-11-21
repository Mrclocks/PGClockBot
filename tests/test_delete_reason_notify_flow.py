"""Prove delete reason survives form POST and is included in user notify."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.datastructures import FormData

from app.services.delete_reason import delete_reason_too_short, extract_delete_reason

ROOT = Path(__file__).resolve().parents[1]


class ExtractDeleteReasonFormTests(unittest.TestCase):
    def test_empty_then_filled_reason(self):
        form = FormData([("reason", ""), ("reason", "حذف تست کاربر")])
        self.assertEqual(extract_delete_reason(form), "حذف تست کاربر")
        self.assertFalse(delete_reason_too_short(extract_delete_reason(form)))

    def test_filled_then_empty_reason_still_finds_value(self):
        """Regression: Starlette .get is last-wins; empty trailing must not win."""
        form = FormData([("reason", "حذف به دلیل تست"), ("reason", "")])
        self.assertEqual(extract_delete_reason(form), "حذف به دلیل تست")

    def test_confirm_reason_backup(self):
        form = FormData([("reason", ""), ("confirm_reason", "علت از مودال")])
        self.assertEqual(extract_delete_reason(form), "علت از مودال")

    def test_too_short_rejected(self):
        self.assertTrue(delete_reason_too_short("اب"))
        self.assertFalse(delete_reason_too_short("حذف"))
        self.assertFalse(delete_reason_too_short("abc"))


class DeleteNotifyIncludesReasonTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_delete_message_contains_reason(self):
        from app.services.notifications import format_account_edit_subject

        text = format_account_edit_subject(
            event="user_delete", reason="درخواست خود کاربر"
        )
        self.assertIn("حذف", text)
        self.assertIn("درخواست خود کاربر", text)
        self.assertIn("علت", text)

    async def test_notify_account_edit_passes_reason_to_user_message(self):
        from app.db.models import BotUser
        from app.services.notifications import notify_account_edit

        user = BotUser(id=7, telegram_id=777, full_name="Test", role="user", username="t")
        session = AsyncMock()
        captured = {}

        async def capture_send(session_, user_, text, **kwargs):
            captured["text"] = text
            return True

        with patch(
            "app.services.notifications.notify_enabled", new=AsyncMock(return_value=True)
        ), patch(
            "app.services.notifications._send_to_user_chat", new=capture_send
        ), patch(
            "app.services.notifications._send_admins", new=AsyncMock()
        ), patch("app.bot.create_bot") as create_bot:
            bot = MagicMock()
            bot.session = MagicMock()
            bot.session.close = AsyncMock()
            create_bot.return_value = bot
            result = await notify_account_edit(
                session,
                user=user,
                event="user_delete",
                reason="تخلف تکرارشده",
                actor="owner",
            )
        self.assertTrue(result.get("subject"))
        self.assertIn("تخلف تکرارشده", captured["text"])
        self.assertIn("علت", captured["text"])

    async def test_users_delete_handler_extracts_reason_before_notify(self):
        """Static+behavioral: handler wires extract → notify(reason=) → delete."""
        api = (ROOT / "app/api/user_list_pages.py").read_text(encoding="utf-8")
        block = api.split("async def users_delete", 1)[1].split("\n    @app.", 1)[0]
        self.assertIn("extract_delete_reason(form)", block)
        self.assertIn("delete_reason_too_short(reason)", block)
        self.assertIn("reason=reason", block)
        self.assertIn('event="user_delete"', block)
        self.assertLess(block.find("notify_account_edit"), block.find("delete_bot_user"))


class PanelJsApplyReasonTests(unittest.TestCase):
    def test_apply_reason_then_body_level_submit_form_post(self):
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertIn("update existing hidden reason in place", js)
        # Delete-form success path inside setupPanelConfirm
        success = js.split("applyReason(form, opts, result.reason);")[1].split(
            "document.addEventListener('click'"
        )[0]
        self.assertIn("submitFormPost(form, overrides)", success)
        self.assertIn("overrides.confirm_reason", success)
        self.assertNotIn("requestSubmit(", success)
        # Executable native submit of the (possibly display:none) origin form must go.
        code_only = "\n".join(
            ln for ln in success.splitlines()
            if not ln.strip().startswith("/*") and "*/" not in ln
        )
        self.assertNotIn("form.submit();", code_only)
        helper = js.split("function submitFormPost(")[1].split(
            "window.panelSubmitFormPost"
        )[0]
        self.assertIn("URLSearchParams", helper)
        self.assertIn("window.fetch(action", helper)
        self.assertNotIn("requestSubmit", helper)


class DeleteReasonCharsetTests(unittest.TestCase):
    """Any script (Persian, Latin, digits, mixed) of length >= 3 is accepted."""

    def test_persian_latin_mixed_accepted(self):
        from starlette.datastructures import FormData

        from app.services.delete_reason import delete_reason_too_short, extract_delete_reason

        cases = [
            "حذف کاربر",
            "abc",
            "AB12",
            "حذف test",
            "۱۲۳۴",  # Persian digits — still characters
            "ممنوع — spam",
        ]
        for text in cases:
            with self.subTest(text=text):
                form = FormData([("reason", text)])
                got = extract_delete_reason(form)
                self.assertEqual(got, text)
                self.assertFalse(delete_reason_too_short(got))

    def test_notify_message_keeps_persian_and_latin_reason(self):
        from app.services.notifications import format_account_edit_subject

        for reason in ("تخلف تکرارشده", "policy violation", "حذف / ban"):
            text = format_account_edit_subject(event="user_delete", reason=reason)
            self.assertIn(reason, text)
            self.assertIn("علت", text)


if __name__ == "__main__":
    unittest.main()
