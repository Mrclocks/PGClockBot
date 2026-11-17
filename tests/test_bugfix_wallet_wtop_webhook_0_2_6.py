"""Regression tests for confirmed wallet / webhook / custom-plan bugs."""

from __future__ import annotations

import re
import unittest


class WtopCallbackShapeTests(unittest.TestCase):
    """Wallet top-up picker must match shop-style wtop:method:0:dest_id callbacks."""

    PATTERN = re.compile(r"^wtop:(card|gateway|psp|crypto)(?::\d+(?::\w+)?)?$")

    def test_method_only(self):
        for key in ("card", "gateway", "psp", "crypto"):
            self.assertTrue(self.PATTERN.match(f"wtop:{key}"))

    def test_picker_with_dest_id(self):
        self.assertTrue(self.PATTERN.match("wtop:card:0:abc12def3456"))
        self.assertTrue(self.PATTERN.match("wtop:gateway:0:gw1"))
        self.assertTrue(self.PATTERN.match("wtop:crypto:0:deadbeef"))

    def test_rejects_garbage(self):
        self.assertFalse(self.PATTERN.match("wtop:card:0:abc:extra"))
        self.assertFalse(self.PATTERN.match("pay:card:1:x"))

    def test_dest_id_parsed_from_part_3(self):
        parts = "wtop:card:0:abc12def3456".split(":")
        self.assertEqual(parts[1], "card")
        self.assertEqual(parts[3] if len(parts) > 3 else None, "abc12def3456")


class DestinationPickerCallbackTests(unittest.TestCase):
    def test_wtop_prefix_includes_order_id_slot(self):
        from app.services.payment_destinations import destination_picker_rows

        rows = destination_picker_rows(
            "card",
            [{"id": "abcd1234ef00", "number": "6037991111111111", "holder": "Ali"}],
            order_id=0,
            prefix="wtop",
        )
        self.assertEqual(rows[0][0][1], "wtop:card:0:abcd1234ef00")
        self.assertTrue(WtopCallbackShapeTests.PATTERN.match(rows[0][0][1]))


class CustomPlanPriceTests(unittest.TestCase):
    def test_ceil_fractional_gb(self):
        from app.services.orders import calc_custom_plan_price

        self.assertEqual(
            calc_custom_plan_price(gb=1.9, days=30, price_per_gb=1000, price_per_day=500),
            2 * 1000 + 30 * 500,
        )
        self.assertEqual(
            calc_custom_plan_price(gb=1, days=30, price_per_gb=1000, price_per_day=500),
            1000 + 30 * 500,
        )


class PaymentDestinationsImportTests(unittest.TestCase):
    def test_helpers_import_get_setting(self):
        import inspect

        from app.services import payment_destinations as pd

        src = inspect.getsource(pd.get_payment_cards)
        self.assertIn("from app.services.users import get_setting", src)
        src_save = inspect.getsource(pd.save_payment_cards)
        self.assertIn("from app.services.users import set_setting", src_save)
        self.assertTrue(callable(pd.get_payment_cards))
        self.assertTrue(callable(pd.save_payment_cards))


class TelegramWebhookEndpointTests(unittest.TestCase):
    def test_strips_path_from_base(self):
        from app.services.setup_wizard import telegram_webhook_endpoint

        base, path, full = telegram_webhook_endpoint(
            "https://panel.example.com/telegram/webhook",
            "/telegram/webhook",
        )
        self.assertEqual(base, "https://panel.example.com")
        self.assertEqual(path, "/telegram/webhook")
        self.assertEqual(full, "https://panel.example.com/telegram/webhook")

    def test_normalizes_path_missing_slash(self):
        from app.services.setup_wizard import telegram_webhook_endpoint

        base, path, full = telegram_webhook_endpoint(
            "https://panel.example.com",
            "telegram/webhook/",
        )
        self.assertEqual(base, "https://panel.example.com")
        self.assertEqual(path, "/telegram/webhook")
        self.assertEqual(full, "https://panel.example.com/telegram/webhook")

    def test_empty_base(self):
        from app.services.setup_wizard import telegram_webhook_endpoint

        base, path, full = telegram_webhook_endpoint("", "/telegram/webhook")
        self.assertEqual(base, "")
        self.assertEqual(path, "/telegram/webhook")
        self.assertEqual(full, "")


if __name__ == "__main__":
    unittest.main()
