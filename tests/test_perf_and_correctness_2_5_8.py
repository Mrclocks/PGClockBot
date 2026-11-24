"""Release 2.5.8 — performance + correctness guards."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.db.models import OrderStatus, PaymentMethod, Plan


class ShopOrderAttributionTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_order_uses_shop_context_not_sticky_reseller(self):
        from app.services.orders import create_order

        plan = SimpleNamespace(
            id=11,
            is_active=True,
            is_trial=False,
            price=5000,
            owner_reseller_id=42,
        )
        captured: dict = {}

        session = AsyncMock()
        session.add = MagicMock(side_effect=lambda o: captured.setdefault("order", o))
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        with (
            patch("app.services.orders.get_catalog_plan", new=AsyncMock(return_value=plan)),
            patch("app.services.orders.apply_discount", new=AsyncMock(return_value=(0, None))),
            patch("app.services.orders.apply_automatic_discounts", new=AsyncMock()) as automatic_discounts,
            patch("app.services.orders._shop_reseller_id", return_value=42),
            patch("app.services.orders.require_purchase_contact", new=AsyncMock()),
            # The shop's volume ceiling is covered elsewhere; this test is about attribution.
            patch("app.services.orders._assert_plan_volume_available", new=AsyncMock()),
            patch(
                "app.services.orders.Order",
                side_effect=lambda **kw: SimpleNamespace(**kw),
            ),
        ):
            await create_order(
                session,
                user_id=7,
                plan_id=11,
                reseller_id=99,  # sticky attribution from another shop — must be ignored
            )

        order = captured["order"]
        self.assertEqual(order.reseller_id, 42)
        self.assertNotEqual(order.reseller_id, 99)
        self.assertEqual(automatic_discounts.call_args.args[1].reseller_id, 42)

    async def test_get_catalog_plan_hides_other_shop(self):
        from app.services.orders import get_catalog_plan

        plan = SimpleNamespace(id=3, owner_reseller_id=7)
        session = AsyncMock()
        session.get = AsyncMock(return_value=plan)
        with patch(
            "app.services.users.current_shop_reseller_id",
            return_value=42,
        ):
            self.assertIsNone(await get_catalog_plan(session, 3))

    async def test_get_catalog_plan_allows_own_shop(self):
        from app.services.orders import get_catalog_plan

        plan = Plan(id=3, owner_reseller_id=42, price=5000, category_id=None, is_trial=False)
        session = AsyncMock()
        session.get = AsyncMock(return_value=plan)
        session.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: []))
        with patch(
            "app.services.users.current_shop_reseller_id",
            return_value=42,
        ):
            self.assertIs(await get_catalog_plan(session, 3), plan)


class WalletClaimTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_rejects_second_pay(self):
        from app.services.orders import _claim_payable_order

        order = SimpleNamespace(id=5, status=OrderStatus.PENDING.value)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=SimpleNamespace(rowcount=0))
        session.get = AsyncMock(
            return_value=SimpleNamespace(id=5, status=OrderStatus.PAID.value)
        )
        session.refresh = AsyncMock()
        session.no_autoflush = MagicMock()
        session.no_autoflush.__enter__ = MagicMock(return_value=session)
        session.no_autoflush.__exit__ = MagicMock(return_value=False)

        with self.assertRaises(ValueError):
            await _claim_payable_order(
                session, order, payment_method=PaymentMethod.WALLET.value
            )


class SharedAdminAuthTests(unittest.TestCase):
    def test_is_platform_admin_helper(self):
        from app.bot.auth import is_platform_admin
        from app.db.models import Role

        admin = SimpleNamespace(role=Role.ADMIN.value, telegram_id=1)
        user = SimpleNamespace(role="user", telegram_id=999)
        with patch("app.config.get_settings") as gs:
            gs.return_value.admin_ids = {42}
            self.assertTrue(is_platform_admin(admin))
            self.assertFalse(is_platform_admin(user))
            listed = SimpleNamespace(role="user", telegram_id=42)
            self.assertTrue(is_platform_admin(listed))


if __name__ == "__main__":
    unittest.main()
