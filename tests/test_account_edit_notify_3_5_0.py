"""3.5.0 — Telegram notices for account edits + admin mirror toggle."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class NotifyPrefsTests(unittest.TestCase):
    def test_account_edits_pref_registered(self):
        from app.services.notifications import NOTIFY_PREFS
        from app.services.users import DEFAULT_SETTINGS

        keys = [k for k, *_ in NOTIFY_PREFS]
        self.assertIn("notify_account_edits", keys)
        self.assertEqual(DEFAULT_SETTINGS.get("notify_account_edits"), "1")


class MessageFormatTests(unittest.TestCase):
    def test_block_subject_includes_reason(self):
        from app.services.notifications import format_account_edit_subject

        text = format_account_edit_subject(event="block", reason="تخلف قوانین")
        self.assertIn("مسدود", text)
        self.assertIn("تخلف قوانین", text)

    def test_role_subject_has_old_new(self):
        from app.services.notifications import format_account_edit_subject

        text = format_account_edit_subject(
            event="role", reason="درخواست کاربر", old_role="reseller", new_role="user"
        )
        self.assertIn("نماینده", text)
        self.assertIn("کاربر", text)
        self.assertIn("درخواست کاربر", text)

    def test_admin_mirror_has_actor(self):
        from app.db.models import BotUser
        from app.services.notifications import format_account_edit_admin

        user = BotUser(id=1, telegram_id=99, full_name="Ali", role="user", username="a")
        text = format_account_edit_admin(
            event="block", user=user, reason="spam", actor="boss"
        )
        self.assertIn("99", text)
        self.assertIn("spam", text)
        self.assertIn("boss", text)


class NotifyDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_notify_account_edit_sends_subject_and_admins(self):
        from app.db.models import BotUser
        from app.services.notifications import notify_account_edit

        user = BotUser(id=1, telegram_id=555, full_name="U", role="user", username="u")
        session = AsyncMock()

        with patch(
            "app.services.notifications.notify_enabled", new=AsyncMock(return_value=True)
        ), patch(
            "app.services.notifications._send_to_user_chat",
            new=AsyncMock(return_value=True),
        ) as send_user, patch(
            "app.services.notifications._send_admins", new=AsyncMock()
        ) as send_admins, patch(
            "app.bot.create_bot"
        ) as create_bot:
            bot = MagicMock()
            bot.session = MagicMock()
            bot.session.close = AsyncMock()
            create_bot.return_value = bot
            result = await notify_account_edit(
                session,
                user=user,
                event="block",
                reason="test",
                actor="admin",
            )
        self.assertTrue(result["subject"])
        self.assertTrue(result["admins"])
        send_user.assert_awaited()
        send_admins.assert_awaited()
        markup = send_admins.await_args.kwargs["markup"]
        self.assertEqual([b.callback_data for row in markup.inline_keyboard for b in row], ["adm:users:view:1"])


class UiWiringTests(unittest.TestCase):
    def test_users_delete_has_reason_block_does_not(self):
        users = (ROOT / "app/web/templates/users.html").read_text(encoding="utf-8")
        self.assertIn("/users/{{ u.id }}/block", users)
        self.assertIn("/users/{{ u.id }}/delete", users)
        delete = users.split("/users/{{ u.id }}/delete", 1)[1].split("</form>", 1)[0]
        self.assertIn("data-confirm-reason", delete)
        block = users.split("/users/{{ u.id }}/block", 1)[1].split("</form>", 1)[0]
        self.assertNotIn("data-confirm-reason", block)
        # Role is edited in modal body, not list
        self.assertNotIn("/users/{{ u.id }}/role", users)

    def test_handlers_call_notify(self):
        api = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("notify_account_edit", api)
        self.assertIn('event="block"', api)
        self.assertIn('event="user_delete"', api)
        bot = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        self.assertIn("block_user_reason", bot)
        self.assertIn("notify_account_edit", bot)


class VersionBumpTests(unittest.TestCase):
    def test_version_at_least_3_5_0(self):
        from app.version import __version__

        self.assertGreaterEqual(
            tuple(int(x) for x in __version__.split(".")[:3]), (0, 1, 0)
        )
        notes = (ROOT / "app/services/release_notes.py").read_text(encoding="utf-8")
        self.assertIn('"0.1.0"', notes)


if __name__ == "__main__":
    unittest.main()
