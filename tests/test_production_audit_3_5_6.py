"""Regression tests for production audit hardening (3.5.6)."""

from __future__ import annotations

import ast
import hashlib
import secrets
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]


class PaymentRaceSourceTests(unittest.TestCase):
    def test_start_method_payment_uses_atomic_claim(self):
        src = (ROOT / "app/services/orders.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(
            n
            for n in tree.body
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "start_method_payment"
        )
        dump = ast.dump(fn)
        self.assertIn("AWAITING_RECEIPT", dump)
        self.assertIn("rowcount", dump)
        self.assertIn("_PAYABLE_ORDER_STATUSES", dump)

    def test_approve_rejects_already_approved(self):
        src = (ROOT / "app/services/orders.py").read_text(encoding="utf-8")
        self.assertIn("این پرداخت قبلاً تأیید شده", src)
        self.assertIn("order already delivered", src)
        self.assertIn("order already paid", src)

    def test_apply_renewal_has_mutex_and_reseller_pg(self):
        import ast

        src = (ROOT / "app/services/orders.py").read_text(encoding="utf-8")
        functions = {
            node.name: ast.get_source_segment(src, node)
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.AsyncFunctionDef)
        }
        # The atomic PAID -> DELIVERING claim and the reseller panel client are
        # separate steps of the renewal; the orchestrator must call both.
        self.assertIn("DELIVERING", functions["_claim_renewal_order"])
        self.assertIn("get_pg_for_reseller", functions["_renewal_panel_client"])
        self.assertIn("_claim_renewal_order", functions["apply_renewal"])
        self.assertIn("_release_failed_renewal", functions["apply_renewal"])
        self.assertIn("PAID", functions["_release_renewal_claim"])


class DiscountReserveTests(unittest.TestCase):
    def test_shop_uses_apply_discount_to_order(self):
        src = (ROOT / "app/bot/handlers/shop.py").read_text(encoding="utf-8")
        self.assertIn("apply_discount_to_order", src)
        self.assertNotIn("apply_discount(\n        session, code_raw", src)

    def test_apply_discount_to_order_defined(self):
        from app.services.orders import apply_discount_to_order

        self.assertTrue(callable(apply_discount_to_order))


class ShopIsolationWebPaymentsTests(unittest.TestCase):
    def test_admin_cannot_approve_shop_payments(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn(
            "پرداخت‌های فروشگاه فقط توسط نماینده همان فروشگاه تأیید می‌شود",
            src,
        )
        overview = (ROOT / "app/services/bot_overview.py").read_text(encoding="utf-8")
        self.assertIn("Order.reseller_id.is_(None)", overview)


class SecurityHardeningSourceTests(unittest.TestCase):
    def test_csrf_requires_origin_or_referer(self):
        src = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("if not origin and not referer:", src)

    def test_webhook_compares_hashed_secrets(self):
        src = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertIn("hashlib.sha256", src)
        self.assertIn("JSONDecodeError", src)

    def test_webhook_digest_equal_length(self):
        a = hashlib.sha256(b"short").digest()
        b = hashlib.sha256(b"much-longer-secret-token").digest()
        self.assertEqual(len(a), len(b))
        self.assertFalse(secrets.compare_digest(a, b))

    def test_secret_box_rotates_placeholder(self):
        src = (ROOT / "app/services/secret_box.py").read_text(encoding="utf-8")
        self.assertIn("is_placeholder_secret", src)
        self.assertIn("ensure_web_secret", src)

    def test_ensure_web_secret_uses_full_placeholder_set(self):
        src = (ROOT / "app/services/setup_wizard.py").read_text(encoding="utf-8")
        self.assertIn("PLACEHOLDER_SECRETS", src)

    def test_pg_hosts_use_staff_pg(self):
        src = (ROOT / "app/api/pg_pages.py").read_text(encoding="utf-8")
        self.assertIn("await _staff_pg(session, staff)", src)
        # Host create must not call bare get_pg().create_host
        self.assertNotIn("await get_pg().create_host(", src)
        self.assertNotIn("await get_pg().delete_host(", src)


class BotHardeningSourceTests(unittest.TestCase):
    def test_rate_limit_middleware_registered(self):
        src = (ROOT / "app/bot/__init__.py").read_text(encoding="utf-8")
        self.assertIn("RateLimitMiddleware", src)

    def test_stars_pre_checkout_validates_payment(self):
        src = (ROOT / "app/bot/handlers/payments.py").read_text(encoding="utf-8")
        self.assertIn("payment.user_id != db_user.id", src)
        self.assertIn("PaymentMethod.STARS", src)

    def test_closed_ticket_blocked_in_service(self):
        src = (ROOT / "app/services/tickets.py").read_text(encoding="utf-8")
        self.assertIn("تیکت بسته شده است", src)

    def test_free_renew_reverts_on_failure(self):
        src = (ROOT / "app/bot/handlers/services.py").read_text(encoding="utf-8")
        self.assertIn("revert_failed_free_delivery", src)

    def test_pre_checkout_user_extraction(self):
        src = (ROOT / "app/bot/middlewares.py").read_text(encoding="utf-8")
        self.assertIn("pre_checkout_query", src)


class WalletTopupBoundTests(unittest.IsolatedAsyncioTestCase):
    async def test_wallet_topup_rejects_huge_amount(self):
        from app.services.orders import create_wallet_topup

        with self.assertRaises(ValueError):
            await create_wallet_topup(AsyncMock(), 1, 600_000_000)

    async def test_wallet_topup_rejects_tiny_amount(self):
        from app.services.orders import create_wallet_topup

        with self.assertRaises(ValueError):
            await create_wallet_topup(AsyncMock(), 1, 500)


class HasPermEmptyAclTests(unittest.TestCase):
    def test_explicit_empty_perms_not_defaulted(self):
        from app.services.resellers import has_perm

        profile = SimpleNamespace(is_active=True, web_permissions="")
        self.assertFalse(has_perm(profile, "payments"))
        self.assertFalse(has_perm(profile, "plans"))


class ApplyDiscountToOrderGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_non_pending(self):
        from app.db.models import OrderStatus
        from app.services.orders import apply_discount_to_order

        order = MagicMock()
        order.status = OrderStatus.AWAITING_RECEIPT.value
        order.discount_code = None
        with self.assertRaises(ValueError):
            await apply_discount_to_order(AsyncMock(), order, "SAVE10")


if __name__ == "__main__":
    unittest.main()
