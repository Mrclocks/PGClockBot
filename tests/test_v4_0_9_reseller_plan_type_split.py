"""v4.0.9 — reseller apply: choose fixed vs PAYG first, then type-specific plans."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]


class ListActiveFilterTests(unittest.TestCase):
    def test_list_active_accepts_billing_mode(self):
        src = (ROOT / "app/services/resellers.py").read_text(encoding="utf-8")
        fn = src[
            src.find("async def list_active_reseller_plans") : src.find(
                "def normalize_reseller_billing_mode"
            )
        ]
        self.assertIn("billing_mode", fn)
        self.assertIn("ResellerPlan.billing_mode", fn)

    def test_format_detail_fixed_vs_payg(self):
        from app.services.resellers import format_reseller_plan_apply_detail

        fixed = MagicMock(
            description="پلن ثابت",
            price=10000,
            commission_percent=15,
            billing_mode="fixed",
            price_per_gb=0,
            pg_group_ids=None,
        )
        body = format_reseller_plan_apply_detail(fixed, currency="تومان")
        self.assertIn("ثابت", body)
        self.assertIn("کمیسیون", body)
        self.assertNotIn("نرخ مصرف", body)

        payg = MagicMock(
            description="پلن مصرفی",
            price=0,
            commission_percent=0,
            billing_mode="payg",
            price_per_gb=2500,
            pg_group_ids="1,2",
        )
        body2 = format_reseller_plan_apply_detail(payg, currency="تومان")
        self.assertIn("Pay As You Go", body2)
        self.assertIn("نرخ مصرف", body2)
        self.assertIn("گروه‌های پاسارگارد", body2)
        self.assertNotIn("کمیسیون", body2)


class BotApplyFlowTests(unittest.TestCase):
    def test_mode_chooser_callbacks(self):
        src = (ROOT / "app/bot/handlers/reseller.py").read_text(encoding="utf-8")
        self.assertIn('F.data == "resapply:home"', src)
        self.assertIn('F.data.startswith("resapply:mode:")', src)
        self.assertIn("resapply:mode:fixed", src)
        self.assertIn("resapply:mode:payg", src)
        self.assertIn("format_reseller_plan_apply_detail", src)
        # Paid path must inject FSMContext
        buy = src[src.find("async def resapply_buy") : src.find("async def resapply_buy") + 800]
        self.assertIn("state: FSMContext", buy)

    def test_reply_nav_starts_with_mode(self):
        src = (ROOT / "app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
        fn = src[src.find("async def open_reseller_apply") : src.find("async def open_reseller_home")]
        self.assertIn("resapply:mode:fixed", fn)
        self.assertIn("resapply:mode:payg", fn)
        self.assertIn("نوع پلن", fn)
        self.assertNotIn("resapply:plan:", fn)


class WebSplitTests(unittest.TestCase):
    def test_plans_page_has_two_reseller_sections(self):
        html = (ROOT / "app/web/templates/plans.html").read_text(encoding="utf-8")
        self.assertIn('id="reseller-plans-fixed"', html)
        self.assertIn('id="reseller-plans-payg"', html)
        self.assertIn("ثابت (کمیسیون)", html)
        self.assertIn("Pay As You Go", html)
        self.assertIn("payg_reseller_plans", html)
        self.assertIn("fixed_reseller_plans", html)

    def test_kind_labels_distinguish_modes(self):
        html = (ROOT / "app/web/templates/plans.html").read_text(encoding="utf-8")
        self.assertIn("ثابت (کمیسیون)", html)
        self.assertIn("reseller-commission-field", html)
        self.assertIn("reseller-price-per-gb-field", html)


class VersionTests(unittest.TestCase):
    def test_version(self):
        from app.version import __version__

        self.assertEqual(__version__, "4.8.0")
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), "4.8.0")


if __name__ == "__main__":
    unittest.main()
