"""Payment method Persian labels — PSP must not fall back to card-to-card."""

from __future__ import annotations

import unittest

from app.db.models import PaymentMethod
from app.services.payment_method_labels import PAYMENT_METHOD_FA, payment_method_fa


class PaymentMethodLabelTests(unittest.TestCase):
    def test_known_methods(self) -> None:
        self.assertEqual(payment_method_fa(PaymentMethod.PSP.value), "درگاه آنلاین")
        self.assertEqual(payment_method_fa(PaymentMethod.BBC.value), "بزن به کارت")
        self.assertEqual(payment_method_fa(PaymentMethod.CARD.value), "کارت به کارت")
        self.assertEqual(payment_method_fa("wallet"), "کیف پول")

    def test_psp_is_not_card(self) -> None:
        self.assertNotEqual(
            payment_method_fa(PaymentMethod.PSP.value),
            PAYMENT_METHOD_FA[PaymentMethod.CARD.value],
        )

    def test_empty_and_unknown(self) -> None:
        self.assertEqual(payment_method_fa(""), "—")
        self.assertEqual(payment_method_fa(None), "—")
        self.assertEqual(payment_method_fa("custom"), "custom")
        self.assertEqual(payment_method_fa("custom", fallback="دیگر"), "دیگر")


if __name__ == "__main__":
    unittest.main()
