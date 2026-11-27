"""Shop maintenance text: what the bot and the Mini App show while new purchases are closed."""

from __future__ import annotations

import unittest

from app.services.shop_maintenance import (
    FALLBACK_MAINTENANCE_TEXT,
    maintenance_message,
    maintenance_plain_text,
)


class MaintenanceMessageTests(unittest.TestCase):
    def test_off_means_open(self) -> None:
        self.assertIsNone(maintenance_message({"shop_maintenance_enabled": "0", "shop_maintenance_text": "x"}))
        self.assertIsNone(maintenance_plain_text({}))

    def test_saved_text_is_used_when_on(self) -> None:
        ui = {"shop_maintenance_enabled": "1", "shop_maintenance_text": "تا فردا بسته‌ایم"}
        self.assertEqual(maintenance_message(ui), "تا فردا بسته‌ایم")

    def test_empty_or_missing_text_falls_back(self) -> None:
        for text in ("", "   ", None, 5):
            with self.subTest(text=text):
                ui = {"shop_maintenance_enabled": "1", "shop_maintenance_text": text}
                self.assertEqual(maintenance_message(ui), FALLBACK_MAINTENANCE_TEXT)

    def test_mini_app_text_is_plain_and_bounded(self) -> None:
        ui = {"shop_maintenance_enabled": "1", "shop_maintenance_text": "ب" * 500}
        self.assertEqual(maintenance_plain_text(ui), "ب" * 200)
        self.assertEqual(len(maintenance_plain_text(ui, limit=10)), 10)


if __name__ == "__main__":
    unittest.main()
