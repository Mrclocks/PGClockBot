"""Owner dashboard rendering: KPI strip, status tiles (notice colour/escaping), account card and chart data."""

from __future__ import annotations

import unittest


from app.api.app import templates
from app.services.dashboard_trend import empty_trend

ACTION = {
    "pending": 6, "tickets": 2, "failures": 0, "expiring": 0, "low_volume": 0,
    "has_items": False, "entries": [],
}
PERIODS = {
    "day": {"orders": 3, "delivered": 2, "revenue": 4820, "new_users": 37},
    "week": {"orders": 9, "delivered": 8, "revenue": 9000, "new_users": 182},
    "month": {"orders": 30, "delivered": 28, "revenue": 90000, "new_users": 610},
}


def render(**overrides: object) -> str:
    trend = empty_trend(7)
    trend.update(today=4820, today_change_pct=12, total=9000, has_data=True)
    context = {
        "staff": {"role": "admin", "permissions": [], "pg_permissions": []},
        "overview": {
            "bot": {"ok": True, "unchecked": False, "username": "shop_bot", "error": None},
            "nodes": {"ok": True, "overall": "ok", "connected": 3, "total": 3, "error": None},
        },
        "pg_health": {"ok": True, "unchecked": False, "version": "1.2", "error": None},
        "show_pg_nodes": False, "periods": PERIODS, "action_center": ACTION, "trend": trend,
        "wallet_card": None, "update": None, "pg_limits": None, "funnel_enabled": False,
        "funnel": {}, "dashboard_degraded": False, "widgets_deferred": False,
    }
    context.update(overrides)
    return templates.get_template("_home_dash_body.html").render(**context)


class DashboardRenderTests(unittest.TestCase):
    def test_kpi_strip_shows_today_sales_users_receipts_and_tickets(self) -> None:
        html = render()
        for expected in ("۴٬۸۲۰", "۳۷", "۱۸۲", "۱۲", "رسید منتظر تأیید", "تیکت باز"):
            self.assertIn(expected, html)

    def test_receipt_and_ticket_cards_link_only_for_staff_who_can_open_those_pages(self) -> None:
        closed = render(kpi_links={"payments": False, "tickets": False})
        self.assertNotIn('href="/finance?tab=payments"', closed)
        self.assertNotIn('href="/tickets"', closed)
        self.assertIn("رسید منتظر تأیید", closed)
        mixed = render(kpi_links={"payments": False, "tickets": True})
        self.assertNotIn('href="/finance?tab=payments"', mixed)
        self.assertIn('href="/tickets"', mixed)
        self.assertNotIn('href="/tickets"', render())

    def test_kpi_links_follow_the_staff_permissions(self) -> None:
        from app.api.home_pages import kpi_links

        self.assertEqual(kpi_links({"role": "admin", "permissions": []}), {"payments": True, "tickets": True})
        shop_staff = {"role": "reseller_staff", "bot_user_id": 9, "reseller_id": 5, "permissions": ["tickets"]}
        self.assertEqual(kpi_links(shop_staff), {"payments": False, "tickets": True})
        self.assertEqual(kpi_links(None), {"payments": False, "tickets": False})

    def test_today_sales_without_a_sale_yesterday_says_so(self) -> None:
        trend = empty_trend(7)
        trend.update(today=4820, today_change_pct=None, has_data=True)
        html = render(trend=trend)
        self.assertIn("دیروز فروشی نبود", html)
        self.assertNotIn('<span class="kpi-d flat">نسبت به دیروز</span>', html)

    def test_an_empty_work_queue_says_it_is_empty(self) -> None:
        html = render()
        self.assertIn("صف کار خالی است", html)
        stale = dict(ACTION, has_items=True, entries=[])
        self.assertIn("صف کار خالی است", render(action_center=stale))

    def test_update_notice_is_its_own_bar_outside_the_connection_status(self) -> None:
        self.assertNotIn("نسخهٔ جدید", render())
        html = render(update={"update_available": True, "remote_version": "0.4.12"})
        self.assertIn("نسخهٔ جدید", html)
        self.assertIn('href="/settings?tab=update"', html)
        status_row = html.split('class="status-box card"', 1)[1].split("</div>\n</div>", 1)[0]
        self.assertNotIn("نسخهٔ جدید", status_row)
        self.assertLess(html.index("نسخهٔ جدید"), html.index("فروش امروز"))

    def test_unhealthy_services_get_their_own_state_and_text_is_escaped(self) -> None:
        html = render(
            overview={"bot": {"ok": False, "unchecked": False, "username": None, "error": "<img src=x onerror=1>"},
                      "nodes": {"ok": True, "overall": "warn", "connected": 2, "total": 3, "error": None}},
            pg_health={"ok": False, "unchecked": False, "version": None, "error": "قطع"},
            show_pg_nodes=True,
        )
        self.assertIn('data-state="err"', html)
        self.assertIn('data-state="warn"', html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("2/3 متصل", html)

    def test_account_card_appears_only_with_wallet_data_and_links_to_that_account(self) -> None:
        self.assertNotIn("حساب من", render())
        card = {"user_id": 7, "balance": 1200, "balance_fa": "", "services": 3, "nearest_expiry_days": 9}
        html = render(wallet_card=card)
        self.assertIn("حساب من", html)
        self.assertIn("/users/7/edit#user-wallet", html)
        self.assertIn("۹ روز", html)

    def test_chart_data_is_embedded_and_an_empty_trend_shows_the_empty_message(self) -> None:
        self.assertIn("data-sales-chart", render())
        self.assertIn("data-trend=", render())
        empty = render(trend=empty_trend(7))
        self.assertIn("هنوز فروشی", empty)
        self.assertIn("sales-empty", empty)
