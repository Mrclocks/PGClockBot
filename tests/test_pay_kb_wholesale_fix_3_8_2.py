"""3.8.2 — restore main KB after pay/receipt; wholesale tx + delivery fixes."""

from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.db.models import PaymentMethod
from app.services.orders import order_quantity, wallet_purchase_reason
from app.services.wallet import _payment_activity_reason


class WalletPurchaseReasonTests(unittest.TestCase):
    def test_wholesale_reason(self):
        order = SimpleNamespace(id=9, quantity=5, note="wholesale:5")
        self.assertIn("خرید عمده", wallet_purchase_reason(order))
        self.assertIn("5 سرویس", wallet_purchase_reason(order))

    def test_normal_reason(self):
        order = SimpleNamespace(id=3, quantity=1, note=None)
        self.assertEqual(wallet_purchase_reason(order), "خرید سفارش #3")

    def test_renew_reason(self):
        order = SimpleNamespace(id=4, quantity=1, note="renew:12")
        self.assertIn("تمدید", wallet_purchase_reason(order))


class PaymentActivityReasonTests(unittest.TestCase):
    def test_wholesale_card(self):
        order = SimpleNamespace(id=7, quantity=10, note="wholesale:10")
        payment = SimpleNamespace(
            order=order,
            order_id=7,
            method=PaymentMethod.CARD.value,
            amount=1000,
        )
        reason = _payment_activity_reason(payment)
        self.assertIn("خرید عمده", reason)
        self.assertIn("کارت", reason)
        self.assertIn("10", reason)


class OrderQuantityFallbackTests(unittest.TestCase):
    def test_note_fallback(self):
        order = SimpleNamespace(quantity=0, note="wholesale:8")
        self.assertEqual(order_quantity(order), 8)


class SourceContractTests(unittest.TestCase):
    def test_delivery_attaches_buyer_main_kb(self):
        from app.services import delivery as delivery_mod

        src = inspect.getsource(delivery_mod.send_delivery_to_user)
        self.assertIn("_buyer_reply_markup", src)
        helper = inspect.getsource(delivery_mod._buyer_reply_markup)
        self.assertIn("buyer_main_reply_keyboard", helper)

    def test_generic_receipt_restores_main(self):
        from app.bot.handlers import wallet as wallet_h

        src = inspect.getsource(wallet_h.generic_receipt)
        self.assertIn("restore_main_reply", src)
        self.assertNotIn("back_home", src)

    def test_wallet_receipt_always_leaves_cancel_kb(self):
        from app.bot.handlers import wallet as wallet_h

        src = inspect.getsource(wallet_h.wallet_receipt_photo)
        self.assertIn("clear_checkout_nav", src)
        self.assertIn("restore_main_reply", src)

    def test_pay_wallet_clears_checkout_nav(self):
        from app.bot.handlers import shop as shop_h

        src = inspect.getsource(shop_h.pay_wallet_cb)
        self.assertIn("clear_checkout_nav", src)
        self.assertIn("buyer_main_reply_keyboard", src)

    def test_card_awaits_receipt_with_inline_cancel(self):
        from app.bot.handlers import shop as shop_h

        src = inspect.getsource(shop_h.pay_card_cb)
        self.assertIn("_await_order_receipt", src)
        await_src = inspect.getsource(shop_h._await_order_receipt)
        self.assertIn("with_cancel_row", await_src)
        self.assertIn("pay_rcpt", await_src)
        self.assertNotIn("cancel_reply", await_src)

    def test_apply_discount_imported(self):
        from app.bot.handlers import shop as shop_h

        self.assertTrue(hasattr(shop_h, "apply_discount_to_order"))

    def test_wholesale_start_preserves_nav(self):
        from app.bot.handlers import shop as shop_h

        src = inspect.getsource(shop_h.wholesale_start)
        self.assertNotIn("state.clear()", src)
        self.assertIn("wholesale_plan_id=None", src)

    def test_wholesale_buy_does_not_full_clear(self):
        from app.bot.handlers import shop as shop_h

        src = inspect.getsource(shop_h.wholesale_buy)
        self.assertNotIn("await state.clear()", src)

    def test_deliver_order_deletes_orphan_services(self):
        from app.services import orders as orders_mod

        src = inspect.getsource(orders_mod.deliver_order)
        self.assertIn("session.delete(svc)", src)

    def test_list_activity_includes_non_wallet_payments(self):
        from app.services import wallet as wallet_mod

        src = inspect.getsource(wallet_mod.list_activity)
        self.assertIn("PaymentMethod.WALLET", src)
        self.assertIn("is_wallet_topup", src)

    def test_open_wallet_tx_uses_list_activity(self):
        from app.bot.handlers import reply_nav as reply_nav_h

        src = inspect.getsource(reply_nav_h.open_wallet_tx)
        self.assertIn("list_activity", src)


class ListActivityMergeTests(unittest.IsolatedAsyncioTestCase):
    async def test_merges_card_purchase(self):
        from datetime import datetime, timezone

        from app.services.wallet import ActivityLine, list_activity

        wtx = SimpleNamespace(
            id=1,
            amount=-5000,
            reason="خرید سفارش #1",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        order = SimpleNamespace(id=42, quantity=5, note="wholesale:5")
        payment = SimpleNamespace(
            id=9,
            amount=80_000,
            method=PaymentMethod.CARD.value,
            order=order,
            order_id=42,
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        session = MagicMock()
        with (
            patch(
                "app.services.wallet.list_transactions",
                new=AsyncMock(return_value=[wtx]),
            ),
            patch.object(session, "execute", new=AsyncMock()) as exe,
        ):
            result = MagicMock()
            result.scalars.return_value.all.return_value = [payment]
            exe.return_value = result
            lines = await list_activity(session, user_id=1, limit=10)

        self.assertEqual(len(lines), 2)
        self.assertIsInstance(lines[0], ActivityLine)
        # Newer card wholesale first
        self.assertIn("خرید عمده", lines[0].reason)
        self.assertEqual(lines[0].amount, -80_000)
        self.assertEqual(lines[1].amount, -5000)


class BuyerMainKeyboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_as_user_true(self):
        from app.bot.menu_nav import buyer_main_reply_keyboard

        session = MagicMock()
        user = SimpleNamespace(id=1)
        order = SimpleNamespace(reseller_id=55)
        with patch(
            "app.bot.menu_nav.build_main_reply_keyboard",
            new=AsyncMock(return_value=("KB", {"x": 1}, "user")),
        ) as build:
            markup, ui = await buyer_main_reply_keyboard(session, user, order=order,
                is_reseller_bot=False,
                reseller_owner_id=None,
            )
        self.assertEqual(markup, "KB")
        self.assertEqual(ui, {"x": 1})
        kwargs = build.await_args.kwargs
        self.assertTrue(kwargs["as_user"])
        self.assertTrue(kwargs["is_reseller_bot"])
        self.assertEqual(kwargs["reseller_owner_id"], 55)


if __name__ == "__main__":
    unittest.main()
