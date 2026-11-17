"""Regression guards for Production Ready 3.0.0 hardening."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.pasarguard import _sanitize_subscription_url, user_subscription_url


class WalletDebitGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_debit_does_not_credit(self):
        from app.db.models import OrderStatus
        from app.services.orders import pay_with_wallet

        order = MagicMock()
        order.id = 9
        order.amount = 5000
        order.status = OrderStatus.PENDING.value
        order.note = None
        order.user_id = 1
        order.plan_id = 1
        order.reseller_id = None

        user = MagicMock()
        user.id = 1
        session = AsyncMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()
        session.add = MagicMock()
        session.execute = AsyncMock(return_value=SimpleNamespace(rowcount=1))
        session.no_autoflush = MagicMock()
        session.no_autoflush.__enter__ = MagicMock(return_value=session)
        session.no_autoflush.__exit__ = MagicMock(return_value=False)

        with (
            patch(
                "app.services.orders.debit_wallet",
                new=AsyncMock(side_effect=ValueError("موجودی کافی نیست")),
            ) as debit,
            patch("app.services.purchase_contact.get_all_settings", new=AsyncMock(return_value={"purchase_require_contact": "0"})),
            patch("app.services.orders.credit_wallet", new=AsyncMock()) as credit,
            patch("app.services.orders.deliver_order", new=AsyncMock()),
        ):
            with self.assertRaises(ValueError):
                await pay_with_wallet(session, order, user)

        credit.assert_not_awaited()
        debit.assert_awaited_once()


class SubscriptionUrlSanitizeTests(unittest.TestCase):
    def test_rejects_javascript(self):
        self.assertIsNone(_sanitize_subscription_url("javascript:alert(1)"))
        self.assertIsNone(_sanitize_subscription_url("data:text/html,x"))
        self.assertIsNone(user_subscription_url({"subscription_url": "javascript:alert(1)"}))

    def test_allows_https_and_vpn_schemes(self):
        self.assertEqual(
            _sanitize_subscription_url("https://example.com/sub/abc"),
            "https://example.com/sub/abc",
        )
        self.assertTrue(_sanitize_subscription_url("vless://uuid@host:443") or False)


if __name__ == "__main__":
    unittest.main()
