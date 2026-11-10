"""Billing pages render for the signed-in staff and their tables work as phone lists.

On phones ``.table-list-sm`` shows only the cells marked for the list (``li-title`` and the
rest), so a row without them is blank. Every list row must carry a title cell, and an empty
table shows its empty-state row.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from html.parser import HTMLParser
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.payg_charge_pages import REVIEW_PATH, register_payg_charge_pages
from app.db.models import Base
from app.services.cashback_rules import CashbackSettings

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
OWNER = {"role": "admin", "permissions": [], "org_principal_id": 1, "org_depth": 0, "org_parent_id": None,
         "org_status": "active", "web_owner": True, "username": "owner"}
SHOP = {"role": "reseller", "permissions": ["dashboard"], "bot_user_id": 5, "username": "shop"}
L1 = {"role": "principal", "org_depth": 1, "bot_user_id": 5, "permissions": [], "username": "l1"}


class _ListRows(HTMLParser):
    """Collects, per phone-list table body row, the classes of its cells."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0  # nesting of .table-list-sm tables
        self.in_body = False
        self.rows: list[list[set[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "table" and "table-list-sm" in classes:
            self.depth += 1
        elif self.depth and tag == "tbody":
            self.in_body = True
        elif self.depth and self.in_body and tag == "tr":
            self.rows.append([])
        elif self.depth and self.in_body and tag == "td" and self.rows:
            self.rows[-1].append(classes | ({"empty-row"} if values.get("colspan") else set()))

    def handle_endtag(self, tag: str) -> None:
        if tag == "tbody":
            self.in_body = False
        elif tag == "table" and self.depth:
            self.depth -= 1


def list_rows(html: str) -> list[list[set[str]]]:
    parser = _ListRows()
    parser.feed(html)
    return parser.rows


def _request(path: str):
    from starlette.requests import Request

    return Request({"type": "http", "asgi": {"version": "3"}, "http_version": "1.1", "method": "GET",
                    "scheme": "https", "path": path, "raw_path": path.encode(), "query_string": b"",
                    "headers": [(b"host", b"example.test")], "client": ("127.0.0.1", 1),
                    "server": ("example.test", 443), "state": {}})


def page(name: str, path: str, staff: dict, **ctx: object) -> str:
    from app.api.app import render

    return render(_request(path), name, {"staff": staff, **ctx}).body.decode()


def _rule(rule_id: int, **kw: object) -> SimpleNamespace:
    base = dict(id=rule_id, name="<b>rule</b>", mode="percent", percent_bps=1000, fixed_amount=0, max_per_order=None,
                min_order_amount=0, events="new", first_purchase_only=False, allow_with_discount=True,
                customer_max_count=None, customer_max_amount=None, priority=0, enabled=True)
    base.update(kw)
    return SimpleNamespace(**base)


def _pages(filled: bool) -> dict[str, str]:
    usage = SimpleNamespace(limit=500_000, used=120_000, available=380_000, percent=24)
    credit = SimpleNamespace(usage=usage, status="overdue")
    statement = SimpleNamespace(period_start=NOW, charges_total=150_000, amount_due=120_000, due_at=NOW, status="issued")
    charge = SimpleNamespace(id=4, shop_id=5, created_at=NOW, event="create", quantity=2, pg_username="u7",
                             amount=20_000, status="settled")
    row = SimpleNamespace(shop_id=5, label="alpha", balance=-1, credit_limit=1, credit_status="overdue",
                          month_charges=1, open_debt=1, month_cashback=1, debt=1)
    plan = SimpleNamespace(id=4, name="p", payg_basis="traffic", price_per_gb=1500, payg_user_price=0,
                           payg_user_price_per_30d=False, payg_unlimited_price=None)
    money = dict(owner=SimpleNamespace(wallet_balance=0), credit=credit, options={"methods": []},
                 recent=[{"id": 1, "amount": 1, "status": "pending", "status_fa": "x"}] if filled else [],
                 statements=[statement] if filled else [], charges=[charge] if filled else [], next_before=None,
                 topup=None)
    return {
        "review": page("payg_charges_review.html", REVIEW_PATH, OWNER, charges=[charge] if filled else [],
                       shop_names={5: "alpha"}),
        "cashback": page("cashback.html", "/cashback", OWNER, rules=[_rule(3)] if filled else [],
                         settings=CashbackSettings(True, "best", 24, 0), budget_used=0),
        "wallet": page("my_finance.html", "/my-subscription/wallet", SHOP, tab="wallet", **money),
        "credit": page("my_finance.html", "/my-subscription/credit", SHOP, tab="credit", **money),
        "charges": page("my_finance.html", "/my-subscription/charges", SHOP, tab="charges", **money),
        "report": page("payg_report.html", "/finance/payg", OWNER, rows=[row] if filled else [],
                       can_orders=True, can_payments=True),
        "children": page("children_pricing.html", "/resellers/children-pricing", L1, plans=[plan] if filled else [],
                         children=[{"shop_id": 6, "label": "c", "plan_id": 4, "credit_limit": 1}] if filled else [],
                         free_credit=0),
    }


class PhoneListTests(unittest.TestCase):
    def test_every_row_has_a_title_cell_for_the_phone_list(self) -> None:
        for name, html in _pages(filled=True).items():
            rows = list_rows(html)
            with self.subTest(page=name):
                self.assertTrue(rows)
                for cells in rows:
                    self.assertTrue(any("li-title" in c for c in cells), cells)

    def test_an_empty_table_shows_its_empty_row(self) -> None:
        for name, html in _pages(filled=False).items():
            rows = list_rows(html)
            with self.subTest(page=name):
                self.assertTrue(rows)
                for cells in rows:
                    self.assertTrue(any("empty-row" in c for c in cells), cells)

    def test_customer_text_is_escaped(self) -> None:
        html = _pages(filled=True)["cashback"]
        self.assertNotIn("<b>rule</b>", html)

    def test_the_payg_report_keeps_the_finance_tabs_with_its_tab_active(self) -> None:
        html = _pages(filled=True)["report"]
        self.assertIn('href="/finance?tab=reports"', html)
        self.assertRegex(html, r'class="active" href="/finance/payg" aria-current="page"')


class ResellerCreditCardTests(unittest.TestCase):
    def test_statements_list_on_phones_and_credit_is_one_form(self) -> None:
        from jinja2 import ChainableUndefined

        from app.api.app import templates

        env = templates.env.overlay(undefined=ChainableUndefined)
        usage = SimpleNamespace(limit=1, used=0, available=1, percent=0)
        statement = SimpleNamespace(id=9, period_start=NOW, amount_due=1, due_at=NOW, status="issued")
        credit = SimpleNamespace(status="active", usage=usage, account=None, statements=[statement], events=[])
        html = env.get_template("_reseller_edit_body.html").render(
            request=_request("/resellers"), staff=OWNER, is_payg=True, credit=credit,
            user=SimpleNamespace(id=5), profile=SimpleNamespace(user_id=5, billing_suspended_at=None),
        )
        section = html[html.index('id="reseller-credit"'):]
        section = section[:section.index("</section>")]
        rows = list_rows(section)
        self.assertTrue(rows and all(any("li-title" in c for c in cells) for cells in rows))
        self.assertEqual(section.count('action="/resellers/5/credit"'), 1)
        self.assertIn('name="frozen_shown"', section)


class ReviewRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_review_page_is_rendered_for_the_signed_in_owner(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.addAsyncCleanup(engine.dispose)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        seen: dict[str, object] = {}

        def render(_request, name, ctx):
            seen.update(ctx)
            return HTMLResponse(name)

        async def get_db():
            async with Session() as session:
                yield session

        app = FastAPI()
        register_payg_charge_pages(app, render=render, require_admin=lambda: OWNER, get_db=get_db)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get(REVIEW_PATH + "?ok=done")
        self.assertEqual(response.status_code, 200)
        self.assertIs(seen["staff"], OWNER)  # the base layout renders nothing without it
        self.assertEqual(seen["flash_ok"], "done")


if __name__ == "__main__":
    unittest.main()
