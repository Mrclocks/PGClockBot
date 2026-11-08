"""Raw exception text never reaches a customer; Persian domain messages pass through."""

from __future__ import annotations

import unittest

from app.services.credential_policy import friendly_pg_error
from app.services.user_errors import GENERIC_ERROR_TEXT, domain_error_text


class DomainErrorTextTests(unittest.TestCase):
    def test_persian_rule_violation_is_shown_as_written(self) -> None:
        self.assertEqual(domain_error_text(ValueError("سرویس پیدا نشد")), "سرویس پیدا نشد")

    def test_stdlib_value_error_is_hidden_behind_a_reference(self) -> None:
        text = domain_error_text(ValueError("invalid literal for int() with base 10: 'x'"))
        self.assertTrue(text.startswith(GENERIC_ERROR_TEXT))
        self.assertIn("E-", text)
        self.assertNotIn("literal", text)

    def test_persian_text_carrying_internal_noise_is_hidden(self) -> None:
        text = domain_error_text(ValueError("خطا: 'NoneType' object has no attribute 'id'"))
        self.assertNotIn("NoneType", text)

    def test_secrets_are_redacted_in_passed_through_text(self) -> None:
        text = domain_error_text(ValueError("ورود ناموفق password=hunter2"))
        self.assertNotIn("hunter2", text)

    def test_each_hidden_error_gets_its_own_reference(self) -> None:
        first = domain_error_text(RuntimeError("boom"))
        second = domain_error_text(RuntimeError("boom"))
        self.assertNotEqual(first, second)

    def test_long_text_is_bounded(self) -> None:
        self.assertLessEqual(len(domain_error_text(ValueError("ب" * 5000))), 190)

    def test_text_fits_a_telegram_callback_alert(self) -> None:
        self.assertLess(len(domain_error_text(ValueError("ب" * 250))), 200)


class PanelNotFoundTextTests(unittest.TestCase):
    def test_404_says_the_item_is_missing_or_removed(self) -> None:
        self.assertIn("حذف", friendly_pg_error("", status_code=404))


if __name__ == "__main__":
    unittest.main()
