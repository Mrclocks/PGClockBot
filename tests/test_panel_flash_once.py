"""Page flashes must render once in base.html above the title, not again under it."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from tests.test_billing_pages_design import OWNER, SHOP, L1, page
from app.services.cashback_rules import CashbackSettings


class PageFlashOnceTests(unittest.TestCase):
    def test_cashback_render_shows_a_single_ok_flash(self):
        html = page(
            "cashback.html",
            "/cashback",
            OWNER,
            flash_ok="تنظیمات کش‌بک ذخیره شد",
            rules=[],
            settings=CashbackSettings(True, "best", 24, 0),
            budget_used=0,
            marketing_sections=(),
        )
        self.assertEqual(html.count('<div class="flash ok">'), 1)
        self.assertIn("تنظیمات کش‌بک ذخیره شد", html)

    def test_my_finance_render_shows_a_single_ok_flash(self):
        usage = SimpleNamespace(limit=0, used=0, available=0, percent=0)
        credit = SimpleNamespace(usage=usage, status="active")
        html = page(
            "my_finance.html",
            "/my-subscription/wallet",
            SHOP,
            flash_ok="ذخیره شد",
            tab="wallet",
            owner=SimpleNamespace(wallet_balance=0),
            credit=credit,
            options={"methods": []},
            recent=[],
            statements=[],
            charges=[],
            next_before=None,
            topup=None,
        )
        self.assertEqual(html.count('<div class="flash ok">'), 1)

    def test_children_pricing_render_shows_a_single_ok_flash(self):
        html = page(
            "children_pricing.html",
            "/resellers/children-pricing",
            L1,
            flash_ok="پلن ذخیره شد",
            plans=[],
            children=[],
            free_credit=0,
        )
        self.assertEqual(html.count('<div class="flash ok">'), 1)


if __name__ == "__main__":
    unittest.main()
