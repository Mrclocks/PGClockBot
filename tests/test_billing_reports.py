"""Owner PAYG report: grouped figures per shop, owner only, CSV safe for spreadsheets."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.app import templates
from app.api.billing_report_pages import register_billing_report_pages
from app.db.models import (
    Base,
    BotUser,
    CashbackGrant,
    CreditStatement,
    ResellerCharge,
    ResellerProfile,
    ShopCreditAccount,
)
from app.services.billing_reports import rows_csv, shop_finance_rows

MONTH = datetime(2026, 11, 1, tzinfo=timezone.utc)


def charge(shop: int, n: int, amount: int, *, status: str = "settled", at: datetime = MONTH) -> ResellerCharge:
    return ResellerCharge(shop_id=shop, event="create", status=status, request_key=f"order:{shop}{n:04d}", attempt=1,
                          quantity=1, amount=amount, idempotency_key=f"payg:{shop}:order:{n}#1", created_at=at)


class ReportCase(unittest.IsolatedAsyncioTestCase):
    owner = True

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=5, telegram_id=5, referral_code="a", wallet_balance=-7_000),
                BotUser(id=6, telegram_id=6, referral_code="b", wallet_balance=2_000),
                BotUser(id=7, telegram_id=7, referral_code="c", wallet_balance=1),
                ResellerProfile(id=1, user_id=5, billing_mode="payg", pg_admin_username="=HYPERLINK(1)"),
                ResellerProfile(id=2, user_id=6, billing_mode="payg", pg_admin_username="shop_b"),
                ResellerProfile(id=3, user_id=7, billing_mode="fixed", pg_admin_username="fixed"),
                ShopCreditAccount(shop_id=5, credit_limit=20_000, status="overdue"),
                charge(5, 1, 3_000), charge(5, 2, 4_000), charge(5, 3, 9_999, status="refunded"),
                charge(5, 4, 5_000, at=MONTH - timedelta(days=2)), charge(6, 1, 1_000),
                CreditStatement(shop_id=5, period_start=MONTH, period_end=MONTH, amount_due=6_000, due_at=MONTH),
                CashbackGrant(shop_id=6, user_id=7, order_id=1, base_amount=1, amount=500, status="available",
                              available_at=MONTH, created_at=MONTH),
            ])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()


class FigureTests(ReportCase):
    async def test_figures_are_grouped_per_shop_and_exclude_refunds_and_other_months(self) -> None:
        async with self.Session() as session:
            rows = {r.shop_id: r for r in await shop_finance_rows(session, month_start=MONTH)}
        self.assertEqual(set(rows), {5, 6})  # fixed shops are not in the PAYG report
        a, b = rows[5], rows[6]
        self.assertEqual((a.debt, a.credit_limit, a.credit_status, a.month_charges, a.open_debt), (7_000, 20_000, "overdue", 7_000, 6_000))
        self.assertEqual((b.debt, b.month_charges, b.month_cashback), (0, 1_000, 500))

    async def test_csv_neutralises_formulas(self) -> None:
        async with self.Session() as session:
            text = rows_csv(await shop_finance_rows(session, month_start=MONTH))
        self.assertIn("'=HYPERLINK(1)", text)
        self.assertIn("'-7000", text)
        self.assertNotIn(",=HYPERLINK", text)


class AccessTests(ReportCase):
    async def client(self, *, owner: bool) -> httpx.AsyncClient:
        app = FastAPI()

        async def get_db():
            async with self.Session() as session:
                yield session

        async def require_admin() -> dict:
            if not owner:
                raise HTTPException(403)
            return {"role": "admin", "web_owner": True}

        def render(request: Request, name: str, ctx: dict) -> HTMLResponse:
            return HTMLResponse(templates.env.get_template(name).render(request=request, csrf_token="t", **ctx))

        register_billing_report_pages(app, render=render, require_admin=require_admin, get_db=get_db)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def test_owner_sees_the_page_and_csv_and_others_do_not(self) -> None:
        async with await self.client(owner=True) as client:
            page = await client.get("/finance/payg")
            csv_response = await client.get("/finance/payg.csv")
        self.assertEqual(page.status_code, 200)
        self.assertIn("shop_b", page.text)
        self.assertNotIn("<script", page.text.split("shop_b")[0][-50:])
        self.assertEqual(csv_response.headers["cache-control"], "no-store")
        async with await self.client(owner=False) as client:
            self.assertEqual((await client.get("/finance/payg")).status_code, 403)
            self.assertEqual((await client.get("/finance/payg.csv")).status_code, 403)


if __name__ == "__main__":
    unittest.main()
