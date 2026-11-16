"""Tests for wholesale QR skip, pending-approval details, copyable formatting."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.formatting import copyable, format_user_label, identity_text, service_card
from app.services.resellers import format_credentials_message


class CopyableTests(unittest.TestCase):
    def test_wraps_code(self):
        self.assertEqual(copyable("https://x.test/a"), "<code>https://x.test/a</code>")

    def test_escapes_html(self):
        self.assertIn("&lt;", copyable("a<b"))

    def test_empty(self):
        self.assertEqual(copyable(""), "—")
        self.assertEqual(copyable(None), "—")


class UserLabelTests(unittest.TestCase):
    def test_shows_name_username_and_id(self):
        u = SimpleNamespace(username="alice", full_name="Alice", telegram_id=123)
        self.assertEqual(format_user_label(u), "Alice · @alice · 123")

    def test_skips_missing_parts(self):
        u = SimpleNamespace(username=None, full_name="علی", telegram_id=9)
        self.assertEqual(format_user_label(u), "علی · 9")

    def test_escapes_hostile_name(self):
        u = SimpleNamespace(username="bob", full_name="<b>x</b>", telegram_id=5)
        self.assertEqual(format_user_label(u), "&lt;b&gt;x&lt;/b&gt; · @bob · 5")

    def test_falls_back_id(self):
        u = SimpleNamespace(username="", full_name="", telegram_id=42)
        self.assertEqual(format_user_label(u), "42")
        self.assertEqual(format_user_label(None, telegram_id=7), "7")


class IdentityTextTests(unittest.TestCase):
    def test_joins_known_parts_and_strips_at_sign(self):
        self.assertEqual(identity_text(full_name="Ali", username="@ali", telegram_id=7), "Ali · @ali · 7")

    def test_unknown_everything_is_dash(self):
        self.assertEqual(identity_text(), "—")


class ServiceCardCopyableTests(unittest.TestCase):
    def test_username_is_code(self):
        card = service_card({"username": "clk_1", "status": "active", "used_traffic": 0})
        self.assertIn("<code>clk_1</code>", card)


class CredentialsCopyableTests(unittest.TestCase):
    def test_urls_are_code(self):
        text = format_credentials_message(
            {
                "unified_credentials": True,
                "panel_username": "shop_abc",
                "panel_password": "Secret1!",
                "panel_url": "https://panel.example",
                "pg_panel_url": "https://pg.example",
                "share_pg_panel_url": True,
            }
        )
        self.assertIn("<code>https://panel.example</code>", text)
        self.assertNotIn("/login", text)
        self.assertIn("<code>shop_abc</code>", text)
        self.assertIn("<code>Secret1!</code>", text)
        self.assertNotIn("ربات اختصاصی (اختیاری)", text)
        self.assertNotIn("rsetup", text)


class WholesaleNoQrTests(unittest.IsolatedAsyncioTestCase):
    async def test_qty_gt_1_sets_skip_qr_and_no_sub_url(self):
        from app.services.delivery import build_delivery_content

        order = SimpleNamespace(id=5, service_id=1, reseller_id=None, quantity=5, note="wholesale:5")
        payment = None
        siblings = [
            SimpleNamespace(
                id=1, pg_username="u1", subscription_url="https://s/1", remark="order:5"
            ),
            SimpleNamespace(
                id=2, pg_username="u2", subscription_url="https://s/2", remark="order:5"
            ),
        ]
        svc = siblings[0]
        session = AsyncMock()

        async def _get(model, pk):
            return svc

        session.get = AsyncMock(side_effect=_get)
        result = MagicMock()
        result.scalars.return_value.all.return_value = siblings
        session.execute = AsyncMock(return_value=result)

        with patch(
            "app.services.delivery.get_all_settings",
            AsyncMock(
                return_value={
                    "qr_enabled": "1",
                    "show_sub_link_in_text": "1",
                    "delivery_title": "OK",
                    "purchase_success_text": "done #{order_id}",
                }
            ),
        ):
            payload = await build_delivery_content(session, payment, order)
        self.assertTrue(payload.get("skip_qr"))
        self.assertIsNone(payload.get("sub_url"))
        self.assertIn("https://s/1", payload["text"])
        self.assertIn("https://s/2", payload["text"])
        self.assertIn("<code>", payload["text"])


class PendingApprovalDetailTests(unittest.IsolatedAsyncioTestCase):
    async def test_includes_plan_and_username(self):
        from app.services.notifications import notify_pending_approval

        user = SimpleNamespace(
            id=3, username="buyer1", full_name="Buyer", telegram_id=111, wallet_balance=0
        )
        plan = SimpleNamespace(id=9, name="الماس ۳۰ گیگ", data_limit_gb=30, duration_days=30)
        order = SimpleNamespace(
            id=77,
            plan_id=9,
            note="wholesale:5",
            quantity=5,
            payment_method="card",
            user_id=3,
            amount=50000,
            reseller_id=None,
        )
        payment = SimpleNamespace(
            id=12,
            amount=50000,
            is_wallet_topup=False,
            order_id=77,
            user_id=3,
            receipt_file_id=None,
        )

        async def _get(model, pk):
            name = getattr(model, "__name__", str(model))
            if "BotUser" in name or model is type(user):
                return user
            # Order / Plan by table
            from app.db.models import BotUser, Order, Plan

            if model is BotUser:
                return user
            if model is Order:
                return order
            if model is Plan:
                return plan
            return None

        session = AsyncMock()
        session.get = AsyncMock(side_effect=_get)
        bot = AsyncMock()
        captured = {}

        async def _dispatch(*args, **kwargs):
            captured["text"] = args[3] if len(args) > 3 else kwargs.get("text")

        with patch(
            "app.services.notifications._dispatch_dual_notify",
            new=AsyncMock(side_effect=_dispatch),
        ):
            await notify_pending_approval(bot, session, payment, 111)

        text = captured.get("text") or ""
        self.assertIn("@buyer1", text)
        self.assertIn("خرید عمده", text)
        self.assertIn("الماس ۳۰ گیگ", text)
        self.assertIn("بابت", text)


if __name__ == "__main__":
    unittest.main()
