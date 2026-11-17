"""Users quick actions + deep-link reuse (v8.2.12)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock


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


class SendStaffDmScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_rejected(self):
        from app.services.users_quick import send_staff_dm

        user = SimpleNamespace(is_blocked=True, telegram_id=1, reseller_id=None)
        with self.assertRaises(ValueError):
            await send_staff_dm(AsyncMock(), user, "hi", shop_id=None)


if __name__ == "__main__":
    unittest.main()
