"""Security tests for Mini App full commerce + ops role isolation."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
COMMERCE = (ROOT / "app/api/miniapp_commerce.py").read_text(encoding="utf-8")
ACL = (ROOT / "app/services/miniapp_acl.py").read_text(encoding="utf-8")
PAGES = (ROOT / "app/api/miniapp_pages.py").read_text(encoding="utf-8")
UPLOADS = (ROOT / "app/services/receipt_uploads.py").read_text(encoding="utf-8")


class MiniCommerceSecuritySourceTests(unittest.TestCase):
    def test_commerce_module_registered(self):
        self.assertIn("register_miniapp_commerce", PAGES)
        self.assertIn("from app.api.miniapp_commerce import register_miniapp_commerce", PAGES)

    def test_order_pay_requires_owner(self):
        chunk = COMMERCE.split("async def mini_pay_order")[1].split("async def ")[0]
        self.assertIn("order.user_id", chunk)
        self.assertIn("user.id", chunk)
        self.assertIn("order.reseller_id is not None", chunk)

    def test_receipt_requires_owner(self):
        chunk = COMMERCE.split("async def mini_attach_receipt")[1].split("async def ")[0]
        self.assertIn("payment.user_id", chunk)
        self.assertIn("save_mini_receipt_upload", chunk)

    def test_receipt_private_store(self):
        self.assertIn("private/receipts", UPLOADS)
        self.assertIn("LOCAL_PREFIX", UPLOADS)
        self.assertIn('".."', UPLOADS)  # traversal guard in split check
        self.assertIn("public media", UPLOADS)

    def test_ops_review_uses_acl(self):
        chunk = COMMERCE.split("async def mini_ops_review")[1].split("async def ")[0]
        self.assertIn("mini_can_review_payment", chunk)
        self.assertIn("load_ops_context", chunk)

    def test_ops_customers_scoped(self):
        chunk = COMMERCE.split("async def mini_ops_customers")[1].split("async def ")[0]
        self.assertIn("BotUser.reseller_id.is_(None)", chunk)
        self.assertIn("profile.user_id", chunk)
        self.assertIn("mini_can_manage_customer", chunk)

    def test_admin_wallet_hidden_from_customer_list(self):
        chunk = COMMERCE.split("async def mini_ops_customers")[1].split("async def ")[0]
        # Reseller must not receive wallet balances of customers
        self.assertIn('if persona == "admin"', chunk)
        self.assertIn('"wallet": int(c.wallet_balance or 0) if persona == "admin" else None', chunk)

    def test_coaching_reseller_only(self):
        chunk = COMMERCE.split("async def mini_ops_coaching")[1].split("async def ")[0]
        self.assertIn('persona != "reseller"', chunk)

    def test_acl_wallet_topup_admin_only(self):
        from app.services.miniapp_acl import mini_can_review_payment

        payment = SimpleNamespace(is_wallet_topup=True)
        self.assertTrue(
            mini_can_review_payment(
                persona="admin",
                reviewer=SimpleNamespace(),
                payment=payment,
                order=None,
                profile=None,
            )
        )
        self.assertFalse(
            mini_can_review_payment(
                persona="reseller",
                reviewer=SimpleNamespace(),
                payment=payment,
                order=None,
                profile=SimpleNamespace(user_id=9),
            )
        )

    def test_acl_admin_cannot_review_shop_order(self):
        from app.services.miniapp_acl import mini_can_review_payment

        payment = SimpleNamespace(is_wallet_topup=False)
        order = SimpleNamespace(reseller_id=42)
        self.assertFalse(
            mini_can_review_payment(
                persona="admin",
                reviewer=SimpleNamespace(),
                payment=payment,
                order=order,
                profile=None,
            )
        )

    def test_acl_reseller_only_own_shop_order(self):
        from app.services.miniapp_acl import mini_can_review_payment
        from unittest.mock import patch

        payment = SimpleNamespace(is_wallet_topup=False)
        order_own = SimpleNamespace(reseller_id=7)
        order_other = SimpleNamespace(reseller_id=8)
        profile = SimpleNamespace(user_id=7)
        with patch("app.services.miniapp_acl.has_bot_perm", return_value=True):
            self.assertTrue(
                mini_can_review_payment(
                    persona="reseller",
                    reviewer=SimpleNamespace(),
                    payment=payment,
                    order=order_own,
                    profile=profile,
                )
            )
            self.assertFalse(
                mini_can_review_payment(
                    persona="reseller",
                    reviewer=SimpleNamespace(),
                    payment=payment,
                    order=order_other,
                    profile=profile,
                )
            )

    def test_user_cannot_call_ops_context(self):
        from app.services.miniapp_acl import require_ops_persona
        from fastapi import HTTPException

        with self.assertRaises(HTTPException):
            require_ops_persona(SimpleNamespace(role="user"))

    def test_support_nav_not_for_admin(self):
        from app.api.miniapp_pages import _nav_for

        self.assertNotIn("support", [x["id"] for x in _nav_for("admin")])
        self.assertIn("support", [x["id"] for x in _nav_for("user")])

    def test_platform_catalog_still_enforced(self):
        order_fn = COMMERCE.split("async def mini_create_order")[1].split("async def ")[0]
        self.assertIn("owner_reseller_id is not None", order_fn)


class ReceiptUploadPathTests(unittest.TestCase):
    def test_traversal_rejected(self):
        from app.services.receipt_uploads import resolve_local_receipt_path

        self.assertIsNone(resolve_local_receipt_path("local:private/receipts/../../etc/passwd"))
        self.assertIsNone(resolve_local_receipt_path("local:uploads/evil.jpg"))
        self.assertIsNone(resolve_local_receipt_path("AgACAgQAAxk..."))


class BroadcastSegmentTests(unittest.TestCase):
    def test_segment_labels_present(self):
        from app.services.broadcast import AUDIENCE_LABELS

        for key in ("expiring_soon", "abandoned_cart", "low_traffic"):
            self.assertIn(key, AUDIENCE_LABELS)


if __name__ == "__main__":
    unittest.main()
