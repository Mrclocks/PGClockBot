"""Users quick actions + deep-link reuse (v8.2.12)."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class StaffMessageSanitizeTests(unittest.TestCase):
    def test_sanitize_strips_tags_and_limits(self):
        from app.services.users_quick import MAX_STAFF_MESSAGE_LEN, sanitize_staff_message

        self.assertEqual(sanitize_staff_message("سلام <b>x</b>"), "سلام x")
        with self.assertRaises(ValueError):
            sanitize_staff_message("   ")
        with self.assertRaises(ValueError):
            sanitize_staff_message("x" * (MAX_STAFF_MESSAGE_LEN + 1))

    def test_wrap_includes_title(self):
        from app.services.users_quick import wrap_staff_dm

        out = wrap_staff_dm("متن", actor="admin")
        self.assertIn("پیام پشتیبانی", out)
        self.assertIn("متن", out)
        self.assertIn("<i>از: admin</i>", out)
        self.assertNotIn("<small>", out)

    def test_wrap_telegram_html_tags_only(self):
        """Telegram parse_mode=HTML rejects <small> (admin DM crash)."""
        from app.services.users_quick import wrap_staff_dm

        out = wrap_staff_dm("تست ارسال", actor="Owner")
        for bad in ("<small>", "</small>", "<div>", "<span>"):
            self.assertNotIn(bad, out)
        self.assertIn("<b>", out)
        self.assertIn("<i>", out)


class QuickRenewPickTests(unittest.TestCase):
    def test_pick_soonest(self):
        from datetime import datetime, timedelta, timezone

        from app.services.users_quick import pick_critical_service

        now = datetime(2026, 8, 20, tzinfo=timezone.utc)
        plan_far = SimpleNamespace(duration_days=90, name="far", data_limit_gb=10)
        plan_near = SimpleNamespace(duration_days=30, name="near", data_limit_gb=5)
        a = SimpleNamespace(
            id=1, created_at=now - timedelta(days=1), plan=plan_far, pg_username="a"
        )
        b = SimpleNamespace(
            id=2, created_at=now - timedelta(days=28), plan=plan_near, pg_username="b"
        )
        picked = pick_critical_service([a, b])
        self.assertEqual(picked.id, 2)


class UsersQuickUiTests(unittest.TestCase):
    def test_template_actions(self):
        users = (ROOT / "app/web/templates/users.html").read_text(encoding="utf-8")
        self.assertIn("quick-renew", users)
        self.assertIn("modal-user-message", users)
        self.assertIn("bulk-message", users)
        self.assertIn("can_user_ops", users)

    def test_routes_registered(self):
        pages = (ROOT / "app/api/user_pages.py").read_text(encoding="utf-8")
        self.assertIn('/users/{user_id}/message', pages)
        self.assertIn('/users/{user_id}/quick-renew', pages)
        self.assertIn('/users/bulk-message', pages)
        self.assertIn("require_ops", pages)

    def test_finance_tickets_deep_links(self):
        fin = (ROOT / "app/web/templates/finance.html").read_text(encoding="utf-8")
        self.assertIn('/users?uid={{ o.user.id }}', fin)
        self.assertIn('/users?uid={{ payer.id }}', fin)
        tix = (ROOT / "app/web/templates/tickets.html").read_text(encoding="utf-8")
        self.assertIn('/users?uid={{ row.user_id }}', tix)
        self.assertIn("cell-user-link", tix)

    def test_action_center_low_volume(self):
        ux = (ROOT / "app/services/ux20.py").read_text(encoding="utf-8")
        self.assertIn('filter_key="low_volume"', ux)
        self.assertIn("notified_traffic", ux)
        macros = (ROOT / "app/web/templates/macros.html").read_text(encoding="utf-8")
        self.assertIn("low_volume", macros)

    def test_bot_message_hooks(self):
        kb = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")
        self.assertIn("adm:users:msg:", kb)
        self.assertIn("adm:users:renew:", kb)
        admin = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        self.assertIn("adm_users_message_start", admin)
        self.assertIn("user_message", admin)
        self.assertIn("_deny_if_outside_platform_shop", admin)
        self.assertIn("_platform_shop_user", admin)
        self.assertIn("adm_users_quick_renew", admin)
        res = (ROOT / "app/bot/handlers/reseller.py").read_text(encoding="utf-8")
        self.assertIn("res:usermsg:", res)
        self.assertIn("res:userrenew:", res)

    def test_version(self):
        from app.version import __version__

        parts = [int(x) for x in __version__.split(".")[:3]]
        self.assertGreaterEqual(parts, [8, 2, 12])
        self.assertEqual(
            (ROOT / "VERSION").read_text(encoding="utf-8").strip(), __version__
        )


class SendStaffDmScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_rejected(self):
        from app.services.users_quick import send_staff_dm

        user = SimpleNamespace(is_blocked=True, telegram_id=1, reseller_id=None)
        with self.assertRaises(ValueError):
            await send_staff_dm(AsyncMock(), user, "hi")


if __name__ == "__main__":
    unittest.main()
