"""PAYG suspend + reseller capacity add-ons tests."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.billing import BILLING_MODE_PAYG, payg_purchase_min_wallet
from app.services.reseller_capacity import (
    ALLOWED_EXTRA_GB,
    ALLOWED_EXTRA_USERS,
    plan_allows_buy_extra,
    plan_extra_gb_price,
    plan_renew_price,
)


class PaygSuspendHelpersTests(unittest.IsolatedAsyncioTestCase):
    async def test_min_topup_is_2x_threshold(self):
        from app.services.billing_suspend import min_topup_to_unsuspend

        with patch(
            "app.services.billing_suspend.get_low_balance_threshold",
            AsyncMock(return_value=10_000),
        ):
            need = await min_topup_to_unsuspend(AsyncMock())
        self.assertEqual(need, 20_000)

    async def test_assert_topup_when_suspended(self):
        from app.services.billing import BillingError
        from app.services.billing_suspend import assert_topup_clears_suspend

        profile = MagicMock()
        profile.billing_suspended_at = object()
        with patch(
            "app.services.billing_suspend.min_topup_to_unsuspend",
            AsyncMock(return_value=20_000),
        ):
            with self.assertRaises(BillingError):
                await assert_topup_clears_suspend(AsyncMock(), profile, 5_000)

    def test_purchase_min_still_strict(self):
        self.assertEqual(payg_purchase_min_wallet(10_000), 20_001)

    async def test_buy_extra_gb_rejects_forged_amount(self):
        from app.services.reseller_capacity import buy_extra_gb

        plan = MagicMock(
            allow_buy_extra=True,
            billing_mode="fixed",
            plan_kind="subscription",
            extra_gb_price=1000,
        )
        profile = MagicMock(
            billing_mode="fixed",
            billing_balance=100_000,
            pg_admin_username="r1",
        )
        user = MagicMock(id=1, wallet_balance=1_000_000)
        with self.assertRaises(ValueError):
            await buy_extra_gb(
                AsyncMock(), user=user, profile=profile, plan=plan, gb=999_999, generation=0,
            )


class ResellerCapacityPlanTests(unittest.TestCase):
    def test_allowed_quantities_whitelist(self):
        self.assertEqual(ALLOWED_EXTRA_GB, frozenset({1, 5, 10, 50}))
        self.assertEqual(ALLOWED_EXTRA_USERS, frozenset({1, 5, 10, 20}))
        self.assertNotIn(999_999, ALLOWED_EXTRA_GB)
        self.assertNotIn(999_999, ALLOWED_EXTRA_USERS)

    def test_allow_flag(self):
        plan = MagicMock(
            allow_buy_extra=True,
            billing_mode="fixed",
            plan_kind="subscription",
            extra_gb_price=1500,
            renew_price=0,
            price=50_000,
        )
        self.assertTrue(plan_allows_buy_extra(plan))
        self.assertEqual(plan_extra_gb_price(plan), 1500)
        self.assertEqual(plan_renew_price(plan), 50_000)

    def test_payg_disallows_buy_extra(self):
        plan = MagicMock(
            allow_buy_extra=True,
            billing_mode=BILLING_MODE_PAYG,
            plan_kind="subscription",
        )
        self.assertFalse(plan_allows_buy_extra(plan))

    def test_renew_price_override(self):
        plan = MagicMock(renew_price=12_000, price=50_000)
        self.assertEqual(plan_renew_price(plan), 12_000)

    def test_templates_have_extra_switch(self):
        plans = Path("app/web/templates/plans.html").read_text(encoding="utf-8")
        edit = Path("app/web/templates/reseller_plan_edit.html").read_text(encoding="utf-8")
        self.assertIn("allow_buy_extra", plans)
        self.assertIn("extra_gb_price", plans)
        self.assertIn("allow_buy_extra", edit)
        self.assertIn("extra_user_price", edit)

    def test_keyboard_capacity_entries(self):
        from app.bot.keyboards import _reseller_submenu_entries

        plan_on = MagicMock(
            allow_buy_extra=True,
            billing_mode="fixed",
            plan_kind="subscription",
        )
        profile = MagicMock(billing_mode="fixed", plan=plan_on)
        with patch("app.services.authz.shop_feature_allowed", return_value=True):
            labels = [t for _, t in _reseller_submenu_entries(profile)]
        # Unit buy-extra + catalog packs share one hub button.
        self.assertIn("📦 بسته‌های حجم/کاربر", labels)
        self.assertNotIn("📦 خرید حجم اضافه", labels)
        self.assertNotIn("👤 خرید کاربر اضافه", labels)
        self.assertIn("🔄 تمدید سرویس", labels)

        plan_off = MagicMock(
            allow_buy_extra=False,
            billing_mode="fixed",
            plan_kind="subscription",
        )
        profile.plan = plan_off
        with patch("app.services.authz.shop_feature_allowed", return_value=True):
            labels2 = [t for _, t in _reseller_submenu_entries(profile)]
        self.assertNotIn("📦 خرید حجم اضافه", labels2)
        self.assertIn("📦 بسته‌های حجم/کاربر", labels2)
        self.assertIn("🔄 تمدید سرویس", labels2)

        payg_plan = MagicMock(
            allow_buy_extra=True,
            billing_mode=BILLING_MODE_PAYG,
            plan_kind="subscription",
        )
        profile.plan = payg_plan
        profile.billing_mode = BILLING_MODE_PAYG
        with patch("app.services.authz.shop_feature_allowed", return_value=True):
            labels3 = [t for _, t in _reseller_submenu_entries(profile)]
        self.assertNotIn("📦 خرید حجم اضافه", labels3)
        self.assertNotIn("👤 خرید کاربر اضافه", labels3)
        self.assertIn("📦 بسته‌های حجم/کاربر", labels3)


class SchemaAddonTests(unittest.TestCase):
    def test_model_fields(self):
        from app.db.models import ResellerPlan, ResellerProfile

        self.assertTrue(hasattr(ResellerProfile, "billing_suspended_at"))
        self.assertTrue(hasattr(ResellerPlan, "allow_buy_extra"))
        self.assertTrue(hasattr(ResellerPlan, "extra_gb_price"))

    def test_migrate_script(self):
        src = Path("app/db/session.py").read_text(encoding="utf-8")
        self.assertIn("billing_suspended_at", src)
        self.assertIn("allow_buy_extra", src)
        alem = Path("alembic/versions/0005_payg_suspend_plan_addons.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("billing_suspended_user_ids", alem)


if __name__ == "__main__":
    unittest.main()
