"""The header's action-center modal: its rows (inbox_entries) and the fragment route that fills it."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.api import home_pages
from app.api.app import render
from app.services.inbox_entries import TONE_ERR, TONE_NEUTRAL, TONE_WARN, inbox_entries

SHOP_STAFF = {"role": "reseller", "bot_user_id": 7, "permissions": ["dashboard"], "username": "shop7"}

FULL_CONTEXT = {
    "update": {"update_available": True, "remote_version": "0.7.1", "version": "0.7.0"},
    "ticket_alert": {"title": "یک تیکت خوانده‌نشده", "detail": "پیام جدید", "href": "/tickets", "ticket_id": 42},
    "shop_maintenance": True,
    "capacity_warn": True,
    "customer_alerts_off": "/shop-settings?tab=notifications",
    "payg_risk": {"suspended": [{"label": "الف"}], "review": 2, "low": [{"label": "ب"}, {"label": "پ"}]},
    "action_center": {"entries": [
        {"key": "pending", "title": "3 رسید در انتظار", "detail": "تأیید یا رد", "href": "/finance?tab=payments", "tone": "warn"},
        {"key": "delivery", "title": "1 تحویل ناموفق", "detail": "صف", "href": "/finance?tab=delivery", "tone": "err"},
        {"key": "brand_new", "title": "مورد تازه", "detail": "", "href": "/x", "tone": "loud"},
    ]},
}


class InboxEntriesTests(unittest.TestCase):
    def test_rows_follow_the_inbox_page_order(self) -> None:
        keys = [entry.dismiss_key for entry in inbox_entries(FULL_CONTEXT)]
        self.assertEqual(keys, [
            "update", "ticket_alert", "shop_maintenance", "capacity_warn", "customer_alerts_off",
            "payg_suspended", "", "payg_low", "ac:pending", "ac:delivery", "ac:brand_new",
        ])

    def test_payg_review_cannot_be_hidden(self) -> None:
        review = [e for e in inbox_entries(FULL_CONTEXT) if e.href == "/payg/charges/review"]
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0].dismiss_key, "")

    def test_ticket_row_keeps_its_ticket_for_the_dismissal(self) -> None:
        ticket = inbox_entries(FULL_CONTEXT)[1]
        self.assertEqual(ticket.entity_id, "42")

    def test_tones_and_icons_of_work_queue_rows(self) -> None:
        rows = {e.dismiss_key: e for e in inbox_entries(FULL_CONTEXT)}
        self.assertEqual((rows["ac:pending"].tone, rows["ac:pending"].icon), (TONE_WARN, "receipt"))
        self.assertEqual(rows["ac:delivery"].tone, TONE_ERR)
        self.assertEqual((rows["ac:brand_new"].tone, rows["ac:brand_new"].icon), (TONE_NEUTRAL, "inbox"))
        self.assertEqual(rows["payg_suspended"].tone, TONE_ERR)

    def test_long_reseller_lists_are_cut_after_three_names(self) -> None:
        ctx = {"payg_risk": {"low": [{"label": n} for n in ("الف", "ب", "پ", "ت")]}}
        self.assertEqual(inbox_entries(ctx)[0].detail, "الف، ب، پ…")

    def test_an_empty_context_has_no_rows(self) -> None:
        self.assertEqual(inbox_entries({}), [])
        self.assertEqual(inbox_entries({"update": {"update_available": False}, "payg_risk": {}, "action_center": {}}), [])


def _client(*, staff: dict | None) -> TestClient:
    app = FastAPI()

    async def require_staff(request: Request) -> dict:
        if staff is None:
            raise HTTPException(status_code=401, detail="ورود لازم است")
        return staff

    async def get_db():
        yield None

    home_pages.register_home_pages(
        app, render=render, require_admin=require_staff, require_staff=require_staff, get_db=get_db,
    )
    return TestClient(app)


class InboxPanelRouteTests(unittest.TestCase):
    def test_panel_lists_the_rows_with_dismiss_buttons(self) -> None:
        built = AsyncMock(return_value={**FULL_CONTEXT, "inbox_hidden_by_dismiss": 2})
        with patch.object(home_pages, "build_inbox_context", built):
            resp = _client(staff=SHOP_STAFF).get("/inbox/panel")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["cache-control"], "no-store")
        html = resp.text
        self.assertEqual(html.count('class="inbox-row '), 11)
        self.assertEqual(html.count("data-inbox-dismiss="), 10)
        self.assertIn('data-inbox-dismiss="ticket_alert"', html)
        self.assertIn('data-inbox-entity="42"', html)
        self.assertIn("۳ رسید در انتظار", html)
        self.assertIn("۲ اعلان مخفی شده", html)
        self.assertIn('href="/shop-settings?tab=notifications"', html)

    def test_panel_is_built_for_the_signed_in_staff_only(self) -> None:
        built = AsyncMock(return_value={})
        with patch.object(home_pages, "build_inbox_context", built):
            _client(staff=SHOP_STAFF).get("/inbox/panel?reseller_id=1")
        self.assertIs(built.await_args.args[2], SHOP_STAFF)

    def test_nothing_waiting_shows_the_empty_state(self) -> None:
        with patch.object(home_pages, "build_inbox_context", AsyncMock(return_value={})):
            html = _client(staff=SHOP_STAFF).get("/inbox/panel").text
        self.assertIn('class="empty-state"', html)
        self.assertNotIn("inbox-row", html)
        self.assertNotIn("اعلان مخفی شده", html)

    def test_reseller_names_are_escaped(self) -> None:
        hostile = {"payg_risk": {"low": [{"label": '<img src=x onerror="alert(1)">'}]}}
        with patch.object(home_pages, "build_inbox_context", AsyncMock(return_value=hostile)):
            html = _client(staff=SHOP_STAFF).get("/inbox/panel").text
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img src=x", html)

    def test_signed_out_request_is_refused(self) -> None:
        built = AsyncMock(return_value={})
        with patch.object(home_pages, "build_inbox_context", built):
            resp = _client(staff=None).get("/inbox/panel")
        self.assertEqual(resp.status_code, 401)
        built.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
