"""Unit tests for unified Billing (RateResolver, traffic source, ledger, gate)."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.billing import (
    BILLING_MODE_FIXED,
    BILLING_MODE_PAYG,
    BillingError,
    PasarGuardAdminTrafficSource,
    RateContext,
    assert_billing_allows_provision,
    bytes_cost_proportional,
    bytes_to_charge_toman,
    is_payg,
)
from app.services.provision_gate import ProvisionError, assert_provision_create


GB = 1024**3


class TrafficSourceTests(unittest.TestCase):
    def test_prefers_lifetime(self):
        src = PasarGuardAdminTrafficSource()
        self.assertEqual(
            src.extract_bytes(
                {"lifetime_used_traffic": 5 * GB, "used_traffic": 1 * GB}
            ),
            5 * GB,
        )

    def test_fallback_used_traffic(self):
        src = PasarGuardAdminTrafficSource()
        self.assertEqual(src.extract_bytes({"used_traffic": 3 * GB}), 3 * GB)

    def test_fallback_traffic_used(self):
        src = PasarGuardAdminTrafficSource()
        self.assertEqual(src.extract_bytes({"traffic_used": 2 * GB}), 2 * GB)

    def test_invalid_returns_none(self):
        src = PasarGuardAdminTrafficSource()
        self.assertIsNone(src.extract_bytes({}))
        self.assertIsNone(src.extract_bytes({"used_traffic": "x"}))


class CostMathTests(unittest.TestCase):
    def test_proportional(self):
        # 0.5 GB at 1000 toman/GB → 500
        self.assertEqual(bytes_cost_proportional(GB // 2, 1000), 500)

    def test_ceil_charge(self):
        self.assertEqual(bytes_to_charge_toman(1, 1000), 1000)
        self.assertEqual(bytes_to_charge_toman(GB, 1000), 1000)
        self.assertEqual(bytes_to_charge_toman(GB + 1, 1000), 2000)

    def test_zero(self):
        self.assertEqual(bytes_cost_proportional(0, 1000), 0)
        self.assertEqual(bytes_cost_proportional(GB, 0), 0)


class ModeHelpersTests(unittest.TestCase):
    def test_is_payg(self):
        p = MagicMock(billing_mode=BILLING_MODE_PAYG)
        self.assertTrue(is_payg(p))
        self.assertFalse(is_payg(MagicMock(billing_mode=BILLING_MODE_FIXED)))
        self.assertFalse(is_payg(None))



class AssertBillingGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_noop_without_reseller(self):
        session = AsyncMock()
        await assert_billing_allows_provision(session, reseller_user_id=None)

    async def test_noop_when_disabled(self):
        session = AsyncMock()
        with patch("app.services.billing.is_billing_enabled", AsyncMock(return_value=False)):
            await assert_billing_allows_provision(session, reseller_user_id=1)

    async def test_noop_for_fixed(self):
        session = AsyncMock()
        with (
            patch("app.services.billing.is_billing_enabled", AsyncMock(return_value=True)),
            patch("app.services.billing.load_payg_profile", AsyncMock(return_value=None)),
        ):
            await assert_billing_allows_provision(session, reseller_user_id=1)

    async def test_blocks_empty_payg(self):
        session = AsyncMock()
        profile = MagicMock(billing_mode=BILLING_MODE_PAYG, billing_balance=0)
        with (
            patch("app.services.billing.is_billing_enabled", AsyncMock(return_value=True)),
            patch("app.services.billing.load_payg_profile", AsyncMock(return_value=profile)),
            patch(
                "app.services.billing.get_on_empty_policy",
                AsyncMock(return_value="block_provision"),
            ),
            patch("app.services.billing.payg_available_balance", AsyncMock(return_value=0)),
            patch(
                "app.services.billing_suspend.suspend_payg_reseller",
                AsyncMock(return_value={}),
            ),
        ):
            with self.assertRaises(BillingError) as ctx:
                await assert_billing_allows_provision(session, reseller_user_id=1)
            self.assertIn("موجودی", ctx.exception.message)

    async def test_allows_positive_balance(self):
        session = AsyncMock()
        profile = MagicMock(billing_mode=BILLING_MODE_PAYG, billing_balance=5000)
        with (
            patch("app.services.billing.is_billing_enabled", AsyncMock(return_value=True)),
            patch("app.services.billing.load_payg_profile", AsyncMock(return_value=profile)),
            patch(
                "app.services.billing.get_on_empty_policy",
                AsyncMock(return_value="block_provision"),
            ),
            patch("app.services.billing.payg_available_balance", AsyncMock(return_value=5000)),
        ):
            await assert_billing_allows_provision(session, reseller_user_id=1)


class ProvisionGateWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_runs_billing_then_quota(self):
        session = AsyncMock()
        with (
            patch(
                "app.services.provision_gate.assert_billing_allows_provision",
                AsyncMock(),
            ) as bill,
            patch(
                "app.services.provision_gate.assert_reseller_can_deliver",
                AsyncMock(),
            ) as quota,
        ):
            await assert_provision_create(
                session,
                reseller_user_id=9,
                pg_admin_username="r1",
                data_limit=GB,
            )
            bill.assert_awaited()
            quota.assert_awaited()

    async def test_billing_error_becomes_provision_error(self):
        session = AsyncMock()
        with patch(
            "app.services.provision_gate.assert_billing_allows_provision",
            AsyncMock(side_effect=BillingError("خالی")),
        ):
            with self.assertRaises(ProvisionError) as ctx:
                await assert_provision_create(
                    session, reseller_user_id=1, pg_admin_username="r1"
                )
            self.assertEqual(ctx.exception.message, "خالی")


class RateResolverSettingFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_to_setting(self):
        from app.services.billing import resolve_price_per_gb

        session = AsyncMock()
        # empty rates table
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result)
        with patch(
            "app.services.users.get_setting",
            AsyncMock(return_value="2500"),
        ):
            price = await resolve_price_per_gb(session, RateContext(reseller_user_id=1))
        self.assertEqual(price, 2500)


class SchemaPresenceTests(unittest.TestCase):
    def test_models_have_billing_fields(self):
        from app.db.models import ResellerBillingRate, ResellerBillingTransaction, ResellerProfile

        self.assertTrue(hasattr(ResellerProfile, "billing_mode"))
        self.assertTrue(hasattr(ResellerProfile, "billing_balance"))
        self.assertTrue(hasattr(ResellerProfile, "billing_watermark_bytes"))
        self.assertEqual(ResellerBillingTransaction.__tablename__, "reseller_billing_transactions")
        self.assertEqual(ResellerBillingRate.__tablename__, "reseller_billing_rates")

if __name__ == "__main__":
    unittest.main()
