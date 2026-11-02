"""The reseller edit body shows PasarGuard usage and the PAYG wallet as stat tiles with a clear suspend warning."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from app.api.app import templates
from app.services.pg_overview import _meter

GB = 1024**3


def _limits(*, users_used: int = 10, traffic_used: int = 5 * GB) -> dict:
    return {
        "ready": True, "username": "<b>shop</b>", "status_label": "فعال", "status_badge": "approved",
        "role_name": "basic", "lifetime_text": "1.79 ترابایت",
        "users": _meter(label="کاربران", used=users_used, limit=20, used_label="", remain_label="", format_value=str),
        "traffic": _meter(label="حجم", used=traffic_used, limit=10 * GB, used_label="", remain_label="",
                          format_value=str, kind="bytes"),
    }


def _render(*, payg: bool, suspended: bool = False, pg_limits: dict | None = None, balance: int = 0) -> str:
    profile = SimpleNamespace(
        billing_suspended_at=datetime.now(timezone.utc) if suspended else None, billing_watermark_bytes=0,
        is_active=True, pg_admin_username="shop", billing_mode="payg" if payg else "fixed",
    )
    return templates.env.get_template("_reseller_edit_body.html").render(
        request=SimpleNamespace(url=SimpleNamespace(path="/resellers"), state=SimpleNamespace(), query_params={}),
        user=SimpleNamespace(id=7, wallet_balance=balance, full_name="r", username="r", telegram_id=1, role="reseller"),
        profile=profile, is_payg=payg, topup_nonce="n" * 16, wallet_balance=0, billing_rate=1500,
        min_unsuspend=40_000, staff={"role": "admin"}, pg_limits=pg_limits,
    )


class ResellerEditUsageTests(unittest.TestCase):
    def test_suspended_payg_reseller_gets_one_warning_with_the_minimum_top_up(self) -> None:
        html = _render(payg=True, suspended=True)
        self.assertEqual(html.count("نماینده به‌خاطر موجودی صفر مسدود است"), 1)
        self.assertIn("40,000", html.split("نماینده به‌خاطر موجودی صفر مسدود است")[1].split("</div>")[0])
        self.assertNotIn("آستانه هشدار", html)
        self.assertEqual(html.count('class="reseller-stat-grid"'), 1)

    def test_active_payg_reseller_has_no_warning(self) -> None:
        html = _render(payg=True)
        self.assertNotIn("مسدود است", html)
        self.assertIn("تومان / گیگ", html)

    def test_usage_shows_escaped_admin_chip_and_lifetime_total(self) -> None:
        html = _render(payg=True, pg_limits=_limits())
        self.assertIn("&lt;b&gt;shop&lt;/b&gt;", html)
        self.assertNotIn("<b>shop</b>", html)
        self.assertIn("مبنای صورتحساب", html)
        self.assertNotIn("مبنای صورتحساب", _render(payg=False, pg_limits=_limits()))

    def test_exhausted_quota_is_called_out(self) -> None:
        self.assertIn("سقف تعداد کاربران پر شده است", _render(payg=False, pg_limits=_limits(users_used=20)))
        self.assertNotIn("سقف تعداد کاربران پر شده است", _render(payg=False, pg_limits=_limits()))

    def test_delete_warning_keeps_the_balance_in_one_line_of_copy(self) -> None:
        html = _render(payg=False, balance=450_000)
        box = html.split("user-edit-wallet-delete-warn-amt")[1].split("</strong>")[0]
        self.assertIn("450,000", box)
        self.assertIn('<div class="flash-copy">', html.split("user-edit-wallet-delete-warn-amt")[0][-200:])
        self.assertNotIn("user-edit-wallet-delete-warn", _render(payg=False, balance=0))


if __name__ == "__main__":
    unittest.main()
