"""PAYG management hardening: wallet gate, differential billing, UI labels."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.billing import (
    BILLING_MODE_PAYG,
    BillingError,
    assert_payg_purchase_wallet,
    bytes_cost_proportional,
    check_payg_purchase_wallet,
    payg_purchase_min_wallet,
)


GB = 1024**3


class PaygWalletGateSyncTests(unittest.TestCase):
    def test_min_is_strictly_above_2x(self):
        self.assertEqual(payg_purchase_min_wallet(10_000), 20_001)
        self.assertEqual(payg_purchase_min_wallet(0), 1)


class PaygWalletGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_gate_shortfall_message(self):
        session = AsyncMock()
        with patch(
            "app.services.billing.get_low_balance_threshold",
            AsyncMock(return_value=10_000),
        ):
            gate = await check_payg_purchase_wallet(session, 5_000)
        self.assertFalse(gate.ok)
        self.assertEqual(gate.required, 20_001)
        self.assertEqual(gate.shortfall, 15_001)
        self.assertIn("کمبود", gate.alert_message)
        self.assertIn("دو برابر", gate.message)

    async def test_gate_ok_when_enough(self):
        session = AsyncMock()
        with patch(
            "app.services.billing.get_low_balance_threshold",
            AsyncMock(return_value=10_000),
        ):
            gate = await check_payg_purchase_wallet(session, 20_001)
        self.assertTrue(gate.ok)
        self.assertEqual(gate.shortfall, 0)

    async def test_assert_raises(self):
        session = AsyncMock()
        with patch(
            "app.services.billing.get_low_balance_threshold",
            AsyncMock(return_value=1000),
        ):
            with self.assertRaises(BillingError):
                await assert_payg_purchase_wallet(session, 100)


class DifferentialBillingMathTests(unittest.TestCase):
    def test_one_gb_then_fraction(self):
        rate = 1000
        first = bytes_cost_proportional(GB, rate)
        # Exact half GB → 500; then another 0.5 → 500 (differential ticks)
        half = bytes_cost_proportional(GB // 2, rate)
        self.assertEqual(first, 1000)
        self.assertEqual(half, 500)
        self.assertEqual(first + half, 1500)

    def test_residual_smaller_than_one_toman(self):
        self.assertEqual(bytes_cost_proportional(1, 1000), 0)


class TickDifferentialTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_tick_bills_only_delta(self):
        from app.services.billing import tick_reseller_usage

        profile = MagicMock()
        profile.billing_mode = BILLING_MODE_PAYG
        profile.user_id = 7
        profile.plan_id = 3
        profile.billing_watermark_bytes = GB

        session = AsyncMock()
        charged = {}

        async def _debit(session, profile, bytes_delta, **kwargs):
            charged["delta"] = bytes_delta
            charged["rate"] = kwargs.get("rate_per_gb")
            return MagicMock(amount=-200)

        with (
            patch(
                "app.services.billing.resolve_price_per_gb",
                AsyncMock(return_value=1000),
            ),
            patch("app.services.billing.debit_usage", side_effect=_debit) as debit,
        ):
            await tick_reseller_usage(
                session,
                profile,
                {"lifetime_used_traffic": int(1.2 * GB)},
                commit=False,
            )
        self.assertEqual(charged["delta"], int(1.2 * GB) - GB)
        debit.assert_awaited()

    async def test_holds_watermark_when_sub_toman(self):
        from app.services.billing import debit_usage

        profile = MagicMock()
        profile.user_id = 9
        profile.billing_watermark_bytes = 0
        session = AsyncMock()
        with patch(
            "app.services.billing._find_by_idempotency",
            AsyncMock(return_value=None),
        ):
            result = await debit_usage(
                session,
                profile,
                bytes_delta=1,
                rate_per_gb=1000,
                watermark_after=1,
                idempotency_key="usage:9:0:1",
                commit=False,
            )
        self.assertIsNone(result)
        session.commit.assert_not_awaited()
        self.assertEqual(profile.billing_watermark_bytes, 0)


class SettingsPaygUiTests(unittest.TestCase):
    def test_tab_renamed_and_no_global_gb_price(self):
        from app.services.users import SETTING_GROUPS, SETTINGS_TABS, TAB_SETTING_GROUPS

        tabs = dict(SETTINGS_TABS)
        self.assertEqual(tabs.get("billing"), "کیف پول و PAYG")
        self.assertIn("مدیریت PAYG", SETTING_GROUPS)
        keys = [item[0] for item in SETTING_GROUPS["مدیریت PAYG"]]
        self.assertNotIn("billing_price_per_gb", keys)
        self.assertIn("billing_low_balance", keys)
        self.assertEqual(TAB_SETTING_GROUPS.get("billing"), ["مدیریت PAYG"])


class VpnLabelRemovalTests(unittest.TestCase):
    def test_pg_users_label_has_no_vpn(self):
        from app.bot.keyboards import _pg_submenu_entries

        labels = [t for _, t in _pg_submenu_entries()]
        self.assertIn("👥 کاربران", labels)
        self.assertTrue(all("VPN" not in t for t in labels))

    def test_nav_pg_prefers_pg_users_label(self):
        from pathlib import Path

        src = Path("app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
        self.assertIn("NAV_ADMIN_PG", src)
        self.assertIn("_pg_submenu_entries", src)


class AdminUsageSnapshotTests(unittest.TestCase):
    def test_snapshot_users_and_traffic(self):
        from app.services.pg_overview import admin_usage_snapshot

        snap = admin_usage_snapshot(
            {
                "total_users": 12,
                "max_users": 50,
                "used_traffic": 2 * GB,
                "data_limit": 10 * GB,
                "lifetime_used_traffic": 15 * GB,
            }
        )
        self.assertTrue(snap["ready"])
        self.assertIn("12", snap["users_text"])
        self.assertIn("50", snap["users_text"])
        self.assertIsNotNone(snap["lifetime_text"])

    def test_resellers_and_admins_templates_show_usage(self):
        from pathlib import Path

        resellers = Path("app/web/templates/resellers.html").read_text(encoding="utf-8")
        admins = Path("app/web/templates/pg_admins.html").read_text(encoding="utf-8")
        self.assertIn("reseller_usage", resellers)
        self.assertIn("users_text", resellers)
        self.assertIn("traffic_text", resellers)
        self.assertIn("کاربران", resellers)
        self.assertIn("حجم", resellers)
        self.assertIn("admin_usage", admins)
        self.assertIn("users_text", admins)
        self.assertIn("traffic_text", admins)
        self.assertIn("کاربران", admins)
        self.assertIn("حجم", admins)


if __name__ == "__main__":
    unittest.main()
