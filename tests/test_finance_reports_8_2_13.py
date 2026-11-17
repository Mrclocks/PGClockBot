"""Finance reports tab + bot hooks (released in v8.5.0)."""

from __future__ import annotations

import unittest


class FinanceReportsNormalizeTests(unittest.TestCase):
    def test_normalize_period(self):
        from app.services.finance_reports import normalize_report_period

        self.assertEqual(normalize_report_period(None), "week")
        self.assertEqual(normalize_report_period(""), "week")
        self.assertEqual(normalize_report_period("DAY"), "day")
        self.assertEqual(normalize_report_period("month"), "month")
        self.assertEqual(normalize_report_period("year"), "week")

    def test_enrich_avg_order(self):
        from app.services.finance_reports import enrich_period_bucket

        self.assertEqual(
            enrich_period_bucket({"delivered": 4, "revenue": 1000})["avg_order"],
            250,
        )
        self.assertEqual(
            enrich_period_bucket({"delivered": 0, "revenue": 100})["avg_order"],
            0,
        )

    def test_telegram_format_escapes(self):
        from app.services.finance_reports import format_finance_report_telegram

        text = format_finance_report_telegram(
            {
                "period": "week",
                "period_label": "۷ روز",
                "generated_at": "2026/08/25 12:00",
                "active": {
                    "revenue": 1000,
                    "delivered": 2,
                    "orders": 3,
                    "avg_order": 500,
                    "new_users": 1,
                },
                "ops": {
                    "total_users": 10,
                    "blocked_users": 0,
                    "no_service_users": 2,
                    "pending_receipts": 1,
                    "open_tickets": 0,
                    "delivery_failures": 0,
                    "expiring_services": 0,
                    "low_volume_services": 0,
                },
                "payment_methods": [
                    {"label": "<script>", "method": "card", "count": 1, "amount": 100}
                ],
                "periods": {
                    "day": {"revenue": 0},
                    "week": {"revenue": 1000},
                    "month": {"revenue": 2000},
                },
            }
        )
        self.assertIn("گزارش مالی", text)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)


if __name__ == "__main__":
    unittest.main()
