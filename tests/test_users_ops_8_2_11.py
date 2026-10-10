"""Users ops list: filters, alert dots, scoped deep-links (v8.2.11)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]


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


class UsersOpsUiTests(unittest.TestCase):
    def test_template_has_ops_chrome(self):
        users = (ROOT / "app/web/templates/users.html").read_text(encoding="utf-8")
        self.assertIn("users-ops-overview", users)
        self.assertIn("alert-dot", users)
        self.assertIn("badge-risk", users)
        self.assertIn("cell-name-tags", users)
        self.assertNotIn("risk-dot", users)
        self.assertNotIn("users-svc-picker", users)
        self.assertIn("filter_key='expiring'", users)
        self.assertIn("نزدیک انقضا", users)
        self.assertNotIn("<th data-sort-type=\"text\">نقش</th>", users)
        self.assertIn("can_manage_users", users)
        # Service, volume and expiry are separate, always-visible columns
        # (desktop AND mobile) — never merged into one cramped cell, never
        # hidden behind col-hide-sm. Multi-service rows still get a full
        # switcher (with live volume/expiry + alert dot); it just lives in
        # its own column now instead of a stacked mini-summary.
        self.assertIn('class="col-svc', users)
        self.assertIn('class="col-vol', users)
        self.assertIn('class="col-exp', users)
        self.assertIn("users-svc-select", users)
        self.assertNotIn("cell-name-meta", users)

        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertIn("users-svc-select-wrap", css)
        self.assertIn(
            "align-items: center",
            css.split(".users-svc-select-wrap", 1)[1].split("}", 1)[0],
        )
        self.assertIn(".alert-dot", css)
        self.assertIn(".users-row.is-focus", css)
        self.assertIn(".cell-name-tags", css)
        self.assertIn(".cell-name-primary", css)
        self.assertNotIn(".users-row.has-alert > td:first-child", css)
        self.assertIn("cell-name-primary", users)
        self.assertIn("sel.id", users)
        self.assertIn(".users-ops-table .col-svc", css)
        self.assertIn(".users-ops-table .col-vol", css)
        self.assertIn(".users-ops-table .col-exp", css)
        self.assertIn(".users-ops-table .col-wallet", css)
        users_html = (ROOT / "app/web/templates/users.html").read_text(encoding="utf-8")
        self.assertIn('class="col-wallet"', users_html)
        self.assertIn("row.wallet_balance", users_html)
        # Wallet stays visible on mobile (not col-hide-sm); # / telegram may still hide.
        wallet_th = users_html.split("کیف پول</th>", 1)[0].rsplit("<th", 1)[-1]
        self.assertNotIn("col-hide-sm", wallet_th)
        self.assertIn("col-hide-sm", users_html)
        self.assertIn("users-svc-boxed", css)
        self.assertIn("users-svc-menu-label", css)
        self.assertNotIn("has-svc-alert", css)
        self.assertIn("data-alert", js)
        self.assertIn("users-svc-menu-label", js)
        self.assertIn("syncUsersSvcToggleAlert", js)
        self.assertIn(".badge.badge-risk", css)
        self.assertNotIn(".risk-dot {", css)

        base = (ROOT / "app/web/templates/base.html").read_text(encoding="utf-8")
        self.assertIn("مشتریان", base)

    def test_action_center_deep_link(self):
        ux = (ROOT / "app/services/ux20.py").read_text(encoding="utf-8")
        self.assertIn("users_list_href", ux)
        self.assertIn('filter_key="expiring"', ux)

    def test_api_scoped_users_page(self):
        api = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("require_perm(\"dashboard\")", api)
        self.assertIn("scoped_users_where", api)
        self.assertIn("build_users_ops_page", api)
        self.assertIn("can_manage_users", api)

    def test_edit_modal_open_hook(self):
        users = (ROOT / "app/web/templates/users.html").read_text(encoding="utf-8")
        self.assertIn('data-modal-open="modal-user-edit"', users)
        self.assertIn("data-modal-load=", users)
        self.assertIn("panel:modal-load", users)
        self.assertNotIn("data-user-edit-open", users)
        self.assertIn("loadEdit", users)
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertIn("panel:modal-load", js)

    def test_bot_parity_hooks(self):
        admin = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        self.assertIn("bot_user_alert_flags", admin)
        self.assertIn("scoped_users_where(None)", admin)
        res = (ROOT / "app/bot/handlers/reseller.py").read_text(encoding="utf-8")
        self.assertIn("bot_user_alert_flags", res)
        self.assertIn("scoped_users_where(int(owner_id))", res)

    def test_version_at_least_package(self):
        from app.version import __version__

        parts = [int(x) for x in __version__.split(".")[:3]]
        self.assertGreaterEqual(parts, [0, 1, 0])
        self.assertEqual(
            (ROOT / "VERSION").read_text(encoding="utf-8").strip(), __version__
        )


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
