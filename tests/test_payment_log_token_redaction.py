"""Payment provider logs identify a checkout token without revealing any part of it."""

from __future__ import annotations

import unittest

from app.services.payment_providers import sunpay as sunpay_mod
from app.services.redact import FINGERPRINT_LENGTH, secret_fingerprint

TOKEN = "Zq7Xw2Lk9Pm4Rt6Yb1Nc8Vd3Hs5Jf0Ga"


class SecretFingerprintTests(unittest.TestCase):
    def test_fingerprint_is_stable_and_short(self) -> None:
        self.assertEqual(secret_fingerprint(TOKEN), secret_fingerprint(TOKEN))
        self.assertEqual(len(secret_fingerprint(TOKEN)), FINGERPRINT_LENGTH)
        self.assertNotEqual(secret_fingerprint(TOKEN), secret_fingerprint(TOKEN + "x"))

    def test_fingerprint_contains_no_part_of_the_secret(self) -> None:
        self.assertNotIn(TOKEN[:6], secret_fingerprint(TOKEN))

    def test_empty_value_has_a_placeholder(self) -> None:
        self.assertEqual(secret_fingerprint(""), "-")
        self.assertEqual(secret_fingerprint(None), "-")


class SunPayVerifyLogTests(unittest.TestCase):
    def _logs(self, data: dict, *, expected_amount: int = 1000) -> str:
        with self.assertLogs(sunpay_mod.logger, level="WARNING") as captured:
            sunpay_mod._parse_verified_payment(data, token=TOKEN, expected_amount=expected_amount)
        return "\n".join(captured.output)

    def test_missing_reference_log_hides_the_checkout_token(self) -> None:
        output = self._logs({"success": True, "data": {"amount": 1000}})
        self.assertNotIn(TOKEN[:8], output)
        self.assertIn(secret_fingerprint(TOKEN), output)

    def test_amount_mismatch_log_hides_the_checkout_token(self) -> None:
        output = self._logs({"success": True, "data": {"amount": 999, "reference": "R1"}})
        self.assertNotIn(TOKEN[:8], output)
        self.assertIn(secret_fingerprint(TOKEN), output)


if __name__ == "__main__":
    unittest.main()
