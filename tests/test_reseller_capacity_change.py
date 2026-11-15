"""The admin's reseller capacity form: amounts plus «کاهش» / «افزایش», like the wallet."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.services.reseller_capacity import capacity_change


class CapacityChangeTests(unittest.TestCase):
    def test_increase_keeps_the_amounts_and_decrease_negates_them(self) -> None:
        self.assertEqual(capacity_change("credit", days=10, gb=50), (10, 50))
        self.assertEqual(capacity_change("debit", days=10, gb=50), (-10, -50))
        self.assertEqual(capacity_change("debit", days=0, gb=5), (0, -5))

    def test_a_negative_amount_with_a_mode_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            capacity_change("credit", days=-3, gb=0)
        with self.assertRaises(ValueError):
            capacity_change("debit", days=0, gb=-1)

    def test_nothing_to_change_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            capacity_change("credit", days=0, gb=0)

    def test_an_unknown_mode_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            capacity_change("double", days=1, gb=1)

    def test_without_a_mode_the_signed_amounts_pass_through(self) -> None:
        self.assertEqual(capacity_change(None, days=-2, gb=7), (-2, 7))


if __name__ == "__main__":
    unittest.main()
