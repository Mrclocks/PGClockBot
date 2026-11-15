"""«کاهش» / «افزایش» day and GB changes from the reseller capacity and service remaining forms."""

from __future__ import annotations

import unittest

from app.services.adjust_mode import signed_adjustment


class SignedAdjustmentTests(unittest.TestCase):
    def test_increase_keeps_the_amounts_and_decrease_negates_them(self) -> None:
        self.assertEqual(signed_adjustment("credit", days=10, gb=50), (10, 50))
        self.assertEqual(signed_adjustment("debit", days=10, gb=50), (-10, -50))
        self.assertEqual(signed_adjustment("debit", days=0, gb=2.5), (0, -2.5))

    def test_a_negative_amount_with_a_mode_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            signed_adjustment("credit", days=-3, gb=0)
        with self.assertRaises(ValueError):
            signed_adjustment("debit", days=0, gb=-0.5)

    def test_nothing_to_change_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            signed_adjustment("credit", days=0, gb=0)

    def test_an_unknown_mode_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            signed_adjustment("double", days=1, gb=1)

    def test_without_a_mode_the_signed_amounts_pass_through(self) -> None:
        self.assertEqual(signed_adjustment(None, days=-2, gb=7), (-2, 7))


if __name__ == "__main__":
    unittest.main()
