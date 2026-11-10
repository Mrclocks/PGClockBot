"""Finance reports tab + bot hooks (released in v8.5.0)."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


class FinanceReportsUiTests(unittest.TestCase):
    def test_finance_reports_first_tab(self):
        html = (ROOT / "app/web/templates/finance.html").read_text(encoding="utf-8")
        # The tab strip lives in its own partial, shared with the PAYG report page.
        html += (ROOT / "app/web/templates/_finance_tabs.html").read_text(encoding="utf-8")
        reports_at = html.find('tab=reports">گزارشات')
        orders_at = html.find('tab=orders">سفارشات')
        self.assertGreater(reports_at, -1)
        self.assertGreater(orders_at, -1)
        self.assertLess(reports_at, orders_at)
        self.assertNotIn("tab=behavior", html)
        self.assertIn('_finance_reports.html', html)

    def test_reports_partial(self):
        partial = (ROOT / "app/web/templates/_finance_reports.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("home-period-grid", partial)
        self.assertIn("finance-report-panels", partial)
        self.assertIn("/users?filter=expiring", partial)
        self.assertIn("/users?filter=low_volume", partial)
        self.assertIn("روش پرداخت", partial)
        # Period compare cards are the selector — no duplicate section-tabs
        self.assertNotIn("finance-report-periods", partial)
        self.assertNotIn('role="tablist" aria-label="بازه گزارش"', partial)

    def test_finance_pages_default_and_scope(self):
        pages = (ROOT / "app/api/finance_pages.py").read_text(encoding="utf-8")
        self.assertIn('"reports"', pages)
        self.assertIn("build_finance_report", pages)
        self.assertIn("shop_owner_id", pages)
        self.assertIn('tab = "reports"', pages)
        # behavior + reports fail-closed when non-platform staff lack shop_owner_id
        self.assertGreaterEqual(pages.count('محدوده فروشگاه مشخص نیست'), 2)
        self.assertIn('tab == "behavior"', pages)

    def test_mobile_css(self):
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(".users-ops-stats", css)
        self.assertNotIn(".finance-report-periods", css)
        self.assertIn(".ui-select-menu.users-svc-boxed", css)
        self.assertIn(".users-ops-table .col-svc", css)
        self.assertIn("bottom: 0", css)
        self.assertIn("tg-preview-gate > .actions", css)

    def test_bot_hooks(self):
        kb = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")
        reply_kb = (ROOT / "app/bot/reply_keyboards.py").read_text(encoding="utf-8")
        self.assertIn("REPLY_ACTION_ADMIN_REPORTS", kb)
        self.assertIn("res_reports", reply_kb)
        admin = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        self.assertIn("adm:reports", admin)
        self.assertIn("build_finance_report", admin)
        self.assertIn("reseller_id=None", admin)
        res = (ROOT / "app/bot/handlers/reseller.py").read_text(encoding="utf-8")
        self.assertIn("res:reports", res)
        self.assertIn("reseller_id=int(owner_id)", res)
        nav = (ROOT / "app/bot/handlers/reply_nav.py").read_text(encoding="utf-8")
        self.assertIn("res:reports:week", nav)
        self.assertIn("REPLY_ACTION_ADMIN_REPORTS", nav)

    def test_service_scoped_doc(self):
        src = (ROOT / "app/services/finance_reports.py").read_text(encoding="utf-8")
        self.assertIn("reseller_id", src)
        self.assertIn("Never fall through", src)

    def test_version(self):
        from app.version import __version__

        parts = [int(x) for x in __version__.split(".")[:3]]
        self.assertGreaterEqual(parts, [0, 1, 0])
        self.assertEqual((ROOT / "VERSION").read_text(encoding="utf-8").strip(), __version__)
        notes = (ROOT / "app/services/release_notes.py").read_text(encoding="utf-8")
        self.assertIn('"0.1.0"', notes)


if __name__ == "__main__":
    unittest.main()
