"""Panel IA: finance hub + support settings domain move."""

from __future__ import annotations

import unittest
from pathlib import Path


class FinanceHubIaTests(unittest.TestCase):
    def test_sidebar_single_orders_destination(self):
        base = Path("app/web/templates/base.html").read_text(encoding="utf-8")
        self.assertIn(">سفارشات</span>", base)
        # No separate primary payments nav item
        self.assertNotIn(">پرداخت‌ها</span>", base)
        self.assertIn("path.startswith('/payments')", base)

    def test_orders_hub_tabs(self):
        src = Path("app/web/templates/orders.html").read_text(encoding="utf-8")
        self.assertIn("finance_tab", src)
        self.assertIn("سفارشات", src)
        self.assertIn("پرداخت‌ها", src)
        self.assertIn("تنظیمات", src)
        self.assertIn("can_orders", src)
        self.assertIn("can_payments", src)
        self.assertIn("can_finance_settings", src)

    def test_payments_redirect_route(self):
        app = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertIn('"/orders?tab=payments"', app)
        self.assertIn("require_any_shop_perm", app)
        # Approve/reject still use dedicated payment ACL routes
        self.assertIn('require_perm("payments")', app)
        self.assertIn('require_perm("orders")', app)

    def test_finance_settings_not_in_bot_settings_tabs(self):
        from app.services.users import (
            SETTINGS_DOMAIN_REDIRECTS,
            SETTINGS_TABS,
            TAB_SETTING_GROUPS,
            keys_for_tab,
        )

        tabs = dict(SETTINGS_TABS)
        self.assertNotIn("payment", tabs)
        self.assertNotIn("billing", tabs)
        self.assertNotIn("supports", tabs)
        self.assertEqual(SETTINGS_DOMAIN_REDIRECTS["payment"], "/orders?tab=settings")
        self.assertEqual(SETTINGS_DOMAIN_REDIRECTS["billing"], "/orders?tab=settings")
        self.assertEqual(SETTINGS_DOMAIN_REDIRECTS["supports"], "/tickets?tab=settings")
        fin = keys_for_tab("finance")
        self.assertIn("card_number", fin)
        self.assertIn("billing_enabled", fin)
        self.assertIn("wallet_success_text", fin)
        self.assertNotIn("welcome_text", fin)
        self.assertIn("مدیریت PAYG", TAB_SETTING_GROUPS["finance"])


class SupportDomainIaTests(unittest.TestCase):
    def test_tickets_have_settings_tab(self):
        src = Path("app/web/templates/tickets.html").read_text(encoding="utf-8")
        self.assertIn("support_tab", src)
        self.assertIn("تیکت‌ها", src)
        self.assertIn("تنظیمات", src)
        self.assertIn("پشتیبان‌های تلگرام", src)

    def test_support_text_group(self):
        from app.services.users import SETTING_GROUPS, keys_for_tab

        keys = [f[0] for f in SETTING_GROUPS["متن پشتیبانی"]]
        self.assertEqual(keys, ["support_text"])
        msg_keys = [f[0] for f in SETTING_GROUPS["متن پیام‌ها"]]
        self.assertNotIn("support_text", msg_keys)
        self.assertNotIn("wallet_success_text", msg_keys)
        self.assertIn("support_text", keys_for_tab("supports"))


class ResellerTabsIaTests(unittest.TestCase):
    def test_reseller_tabs_without_payment_supports(self):
        from app.services.resellers import RESELLER_SETTINGS_TABS

        keys = {k for k, _ in RESELLER_SETTINGS_TABS}
        self.assertNotIn("payment", keys)
        self.assertNotIn("supports", keys)
        self.assertIn("messages", keys)
        self.assertIn("services", keys)


if __name__ == "__main__":
    unittest.main()
