"""Users ops list: filters, alert dots, scoped deep-links (v8.2.11)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


class UsersOpsUnitTests(unittest.TestCase):
    def test_href_whitelist(self):
        from app.services.users_ops import users_list_href

        self.assertEqual(users_list_href(), "/users")
        self.assertEqual(users_list_href(filter_key="expiring"), "/users?filter=expiring")
        self.assertEqual(
            users_list_href(filter_key="expiring", uid=42),
            "/users?filter=expiring&uid=42",
        )
        self.assertEqual(users_list_href(filter_key="hack';drop"), "/users")
        self.assertEqual(users_list_href(uid=-3), "/users")
        self.assertEqual(users_list_href(uid="x"), "/users")

    def test_build_row_expiring_and_alert(self):
        from app.services.users_ops import build_user_ops_row

        now = datetime(2026, 8, 20, tzinfo=timezone.utc)
        plan = SimpleNamespace(name="ماهانه", duration_days=30, data_limit_gb=10.0)
        svc = SimpleNamespace(
            id=7,
            pg_username="u7",
            created_at=now - timedelta(days=28),
            plan=plan,
            notified_traffic=False,
            notified_expire=False,
        )
        user = SimpleNamespace(id=1, is_blocked=False, role="user", risk_flags=None)
        row = build_user_ops_row(user, [svc], expire_days=3, now=now)
        self.assertTrue(row.expiring)
        self.assertTrue(row.has_alert)
        self.assertEqual(row.service_count, 1)
        self.assertIn("گیگ", row.volume_text)
        self.assertRegex(row.expire_text, r"\d+ روز")
        self.assertNotIn("/", row.expire_text)

    def test_low_volume_flag(self):
        from app.services.users_ops import build_user_ops_row

        now = datetime(2026, 8, 20, tzinfo=timezone.utc)
        plan = SimpleNamespace(name="پلن", duration_days=90, data_limit_gb=50.0)
        svc = SimpleNamespace(
            id=1,
            pg_username="x",
            created_at=now - timedelta(days=1),
            plan=plan,
            notified_traffic=True,
            notified_expire=False,
        )
        user = SimpleNamespace(id=2, is_blocked=False, role="user")
        row = build_user_ops_row(user, [svc], expire_days=3, now=now)
        self.assertTrue(row.low_volume)
        self.assertTrue(row.has_alert)
        self.assertFalse(row.expiring)

    def test_filter_and_sort_focus(self):
        from app.services.users_ops import (
            UserOpsRow,
            filter_ops_rows,
            sort_ops_rows,
            summarize_ops_counts,
        )

        def row(uid, *, expiring=False, alert=False, urgency=0):
            r = UserOpsRow(user=SimpleNamespace(id=uid, is_blocked=False))
            r.expiring = expiring
            r.has_alert = alert
            r.urgency = urgency
            return r

        rows = [row(1, urgency=1), row(2, expiring=True, alert=True, urgency=50)]
        self.assertEqual(len(filter_ops_rows(rows, "expiring")), 1)
        ordered = sort_ops_rows(rows, focus_uid=1)
        self.assertEqual(ordered[0].user.id, 1)
        counts = summarize_ops_counts(rows)
        self.assertEqual(counts["expiring"], 1)
        self.assertEqual(counts["alerts"], 1)


class ActionCenterExpiringHrefTests(unittest.IsolatedAsyncioTestCase):
    async def test_expiring_href(self):
        from app.services.ux20 import build_action_center

        now = datetime.now(timezone.utc)
        calls = {"n": 0}

        async def _exec(_q):
            calls["n"] += 1
            m = MagicMock()
            # pending, tickets, failures counts → 0
            # then max(Plan.duration_days) → 30
            # then service scan → one row in window
            if calls["n"] <= 3:
                m.scalar.return_value = 0
                m.all.return_value = []
            elif calls["n"] == 4:
                m.scalar.return_value = 30
                m.all.return_value = []
            else:
                m.scalar.return_value = 0
                m.all.return_value = [(now - timedelta(days=28), 30)]
            return m

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=_exec)
        out = await build_action_center(session, reseller_id=None, expire_days=3)
        self.assertGreaterEqual(out["expiring"], 1)
        exp = next(e for e in out["entries"] if e["key"] == "expiring")
        self.assertEqual(exp["href"], "/users?filter=expiring")


if __name__ == "__main__":
    unittest.main()
