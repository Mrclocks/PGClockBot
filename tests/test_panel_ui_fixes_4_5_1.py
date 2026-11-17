"""Panel UI fixes — volume spacing, Owner kebab, group modal, digits, switches."""

from __future__ import annotations

import unittest

from app.services.formatting import format_bytes, format_bytes_ratio
from app.services.numbers import normalize_number_text, parse_float, parse_int
from tests.template_source import template_with_includes
GB = 1024**3


class VolumeSpacingTests(unittest.TestCase):
    def test_ratio_spaces_around_slash(self):
        text = format_bytes_ratio(int(1.12 * GB), 10 * GB)
        self.assertEqual(text, "1.12 / 10 گیگ")
        self.assertTrue(text.index("1.12") < text.index("گیگ"))
        self.assertNotRegex(text, r"گیگ\s+\d")

    def test_single_value_number_then_unit(self):
        text = format_bytes(10 * GB)
        self.assertEqual(text, "10 گیگ")
        self.assertFalse(text.startswith("گیگ"))


class CancelButtonUnityTests(unittest.TestCase):

    def test_plan_form_modals_have_no_cancel_button(self):
        plans = template_with_includes("plans.html")
        self.assertNotIn('data-modal-close>انصراف</button>', plans)


class DigitNormalizeTests(unittest.TestCase):
    def test_persian_and_arabic(self):
        self.assertEqual(normalize_number_text("۱۲۳"), "123")
        self.assertEqual(normalize_number_text("١٢٣"), "123")
        self.assertEqual(parse_int("۵۰"), 50)
        self.assertAlmostEqual(parse_float("۱٫۵"), 1.5)
        self.assertAlmostEqual(parse_float("١٫١٢"), 1.12)


if __name__ == "__main__":
    unittest.main()
