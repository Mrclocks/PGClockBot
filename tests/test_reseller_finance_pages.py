"""The shop owner's wallet, credit and charges pages: owner only, own purse only, own rows only."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.app import templates
from app.api.reseller_finance_pages import CHARGES_PAGE_SIZE, register_reseller_finance_pages
from app.db.models import Base, BotUser, CreditStatement, ResellerCharge, ResellerProfile
from app.services.wallet_topup import RECEIPT_MAX_BYTES, TopupError, TopupStart

OWNER = {"role": "reseller", "bot_user_id": 5}
NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)


class FinancePagesCase(unittest.IsolatedAsyncioTestCase):
    staff: dict = OWNER

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=5, telegram_id=55, referral_code="a", role="reseller", wallet_balance=-3_000),
                BotUser(id=6, telegram_id=66, referral_code="b", role="reseller"),
                ResellerProfile(id=1, user_id=5, billing_mode="payg"),
                ResellerProfile(id=2, user_id=6, billing_mode="payg"),
                CreditStatement(shop_id=6, period_start=NOW, period_end=NOW, amount_due=999, due_at=NOW),
            ])
            for n in range(CHARGES_PAGE_SIZE + 3):
                session.add(ResellerCharge(shop_id=5, event="create", status="settled", request_key=f"order:{n:04d}",
                                           attempt=1, quantity=1, pg_username=f"mine{n}", amount=1_000,
                                           idempotency_key=f"payg:5:order:{n:04d}#1"))
            session.add(ResellerCharge(shop_id=6, event="create", status="settled", request_key="order:9999",
                                       attempt=1, quantity=1, pg_username="theirs", amount=1_000,
                                       idempotency_key="payg:6:order:9999#1"))
            await session.commit()

        app = FastAPI()

        async def get_db():
            async with self.Session() as session:
                yield session

        async def require_staff() -> dict:
            return dict(self.staff)

        def render(request: Request, name: str, ctx: dict) -> HTMLResponse:
            return HTMLResponse(templates.env.get_template(name).render(request=request, csrf_token="t", **ctx))

        register_reseller_finance_pages(app, render=render, require_staff=require_staff, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        settings = patch("app.api.reseller_finance_pages.get_all_settings", AsyncMock(return_value={}))
        settings.start()
        self.addCleanup(settings.stop)

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.engine.dispose()


class OwnerTests(FinancePagesCase):
    async def test_pages_render_for_the_owner_only_with_own_rows(self) -> None:
        wallet = await self.client.get("/my-subscription/wallet")
        self.assertEqual(wallet.status_code, 200)
        self.assertIn("کیف پول", wallet.text)
        credit = await self.client.get("/my-subscription/credit")
        self.assertNotIn("999", credit.text)  # the other shop's statement
        charges = await self.client.get("/my-subscription/charges")
        self.assertNotIn("theirs", charges.text)
        self.assertIn("mine", charges.text)
        self.assertIn("?before=", charges.text)

    async def test_top_up_goes_to_the_owners_platform_purse(self) -> None:
        start = AsyncMock(return_value=TopupStart(payment_id=7, method="card", amount=50_000, needs_receipt=True,
                                                  card_number="6037-0000"))
        with patch("app.api.reseller_finance_pages.start_topup", start):
            page = await self.client.post("/my-subscription/wallet/topup",
                                          data={"amount": "۵۰٬۰۰۰", "method": "card", "user_id": "6", "shop_id": "6"})
        self.assertEqual(page.status_code, 200)
        kwargs = start.await_args.kwargs
        self.assertEqual((kwargs["user_id"], kwargs["shop_id"], kwargs["amount"]), (5, None, 50_000))
        self.assertIn("6037-0000", page.text)
        self.assertIn("/my-subscription/wallet/topup/7/receipt", page.text)

    async def test_refused_top_up_shows_a_safe_message(self) -> None:
        with patch("app.api.reseller_finance_pages.start_topup", AsyncMock(side_effect=TopupError("مبلغ کم است"))):
            response = await self.client.post("/my-subscription/wallet/topup", data={"amount": "1", "method": "card"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("err=", response.headers["location"])

    async def test_receipt_is_bound_to_the_owner_and_capped(self) -> None:
        submit = AsyncMock(return_value=type("R", (), {"status": "ok", "message": "رسید ثبت شد"})())
        bot = type("B", (), {"session": type("S", (), {"close": AsyncMock()})()})()
        with patch("app.api.reseller_finance_pages.submit_topup_receipt", submit), \
                patch("app.api.reseller_finance_pages.open_notify_bot_for_user", AsyncMock(return_value=(bot, True))):
            big = await self.client.post("/my-subscription/wallet/topup/7/receipt",
                                         files={"receipt": ("r.jpg", b"x" * (RECEIPT_MAX_BYTES + 1), "image/jpeg")})
            self.assertIn("err=", big.headers["location"])
            submit.assert_not_awaited()
            ok = await self.client.post("/my-subscription/wallet/topup/7/receipt",
                                        files={"receipt": ("r.jpg", b"\xff\xd8\xff", "image/jpeg")})
        self.assertIn("ok=", ok.headers["location"])
        kwargs = submit.await_args.kwargs
        self.assertEqual((kwargs["user_id"], kwargs["shop_id"], kwargs["telegram_id"], kwargs["payment_id"]),
                         (5, None, 55, 7))


class NotOwnerTests(FinancePagesCase):
    staff = {"role": "principal", "bot_user_id": 5}

    async def test_other_roles_are_refused(self) -> None:
        for path in ("/my-subscription/wallet", "/my-subscription/credit", "/my-subscription/charges"):
            self.assertEqual((await self.client.get(path)).status_code, 403, path)
        self.assertEqual((await self.client.post("/my-subscription/wallet/topup", data={})).status_code, 403)


if __name__ == "__main__":
    unittest.main()
