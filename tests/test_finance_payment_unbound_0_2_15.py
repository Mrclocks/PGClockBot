"""Regression: finance orders/payments must not 500 after delivery-diag import.

Phase 0 added ``from app.db.models import Payment`` inside the delivery branch of
``finance_page``. That made ``Payment`` a function-local name for the whole
handler, so orders/payments tabs raised UnboundLocalError on ``select(Payment)``
whenever there was at least one order/payment row to load (HTTP 500).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]


class FinancePaymentUnboundSourceGuards(unittest.TestCase):
    def test_no_local_payment_reimport_in_finance_page(self):
        src = (ROOT / "app/api/finance_pages.py").read_text(encoding="utf-8")
        # Module-level import is required; a nested re-import inside finance_page
        # shadows it for orders/payments tabs.
        import app.api.finance_pages as finance_pages
        from app.db.models import Payment

        self.assertIs(finance_pages.Payment, Payment)
        start = src.index("def register_finance_pages")
        # Only the finance_page body matters (through legacy redirects).
        body = src[start : src.index("def _legacy_finance_redirect", start)]
        self.assertNotIn("from app.db.models import Payment", body)


class FinanceOrdersPaymentsLiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="finance-unbound-")
        db_path = Path(self._tmpdir.name) / "t.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        from app.db.models import Base

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

        from app.db.models import (
            BotUser,
            DeliveryFailure,
            Order,
            OrderStatus,
            OrgPrincipal,
            Payment,
            PaymentStatus,
            Plan,
            Role,
        )

        async with self.Session() as session:
            owner = BotUser(
                telegram_id=1001,
                username="owner",
                full_name="Owner",
                role=Role.ADMIN.value,
                referral_code="OWN1001",
            )
            session.add(owner)
            await session.flush()
            op = OrgPrincipal(depth=0, status="active", bot_user_id=owner.id)
            session.add(op)
            await session.flush()
            buyer = BotUser(
                telegram_id=2002,
                username="buyer",
                full_name="Buyer",
                role=Role.USER.value,
                referral_code="BUY2002",
            )
            session.add(buyer)
            await session.flush()
            plan = Plan(name="P", price=100_000, duration_days=30, is_active=True)
            session.add(plan)
            await session.flush()
            o = Order(
                user_id=buyer.id,
                plan_id=plan.id,
                amount=100_000,
                status=OrderStatus.PAID.value,
                payment_method="card",
            )
            session.add(o)
            await session.flush()
            p = Payment(
                order_id=o.id,
                user_id=buyer.id,
                amount=100_000,
                method="card",
                status=PaymentStatus.APPROVED.value,
                receipt_file_id="fid",
            )
            session.add(p)
            await session.flush()
            session.add(
                DeliveryFailure(
                    order_id=o.id,
                    payment_id=p.id,
                    reseller_id=None,
                    error="tg",
                    attempts=1,
                )
            )
            await session.commit()
            self.org_principal_id = int(op.id)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmpdir.cleanup()

    def _app(self) -> FastAPI:
        from app.api.app import render
        from app.api.finance_pages import register_finance_pages

        staff = {
            "role": "admin",
            "telegram_id": 1001,
            "user_id": 1,
            "bot_user_id": 1,
            "username": "owner",
            "full_name": "Owner",
            "org_principal_id": self.org_principal_id,
            "org_depth": 0,
            "org_parent_id": None,
            "org_status": "active",
        }
        app = FastAPI()

        @app.exception_handler(Exception)
        async def _unhandled(_request: Request, exc: Exception):
            return HTMLResponse(f"500 {type(exc).__name__}: {exc}", status_code=500)

        async def require_staff():
            return staff

        async def get_db():
            async with self.Session() as session:
                yield session

        register_finance_pages(
            app, render=render, require_staff=require_staff, get_db=get_db
        )
        return app

    async def test_orders_and_payments_tabs_ok_with_rows(self) -> None:
        transport = httpx.ASGITransport(app=self._app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for path in (
                "/finance?tab=orders",
                "/finance?tab=payments",
                "/finance?tab=delivery",
                "/finance?tab=reports",
            ):
                resp = await client.get(path, follow_redirects=True)
                self.assertEqual(
                    resp.status_code,
                    200,
                    msg=f"{path} -> {resp.status_code}: {resp.text[:240]}",
                )
                self.assertNotIn("UnboundLocalError", resp.text)


if __name__ == "__main__":
    unittest.main()
