"""Gateway return page: cancel and «not confirmed yet» are not shown as a failed payment."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.settlement_pages import register_settlement_pages
from app.db import Base
from app.db.models import (
    BotUser,
    Payment,
    PaymentMethod,
    PaymentSettlement,
    PaymentStatus,
    Role,
    SettlementStatus,
)
from app.services.payment_providers import sunpay as sunpay_mod
from app.services.payment_providers.sunpay import close_client
from app.services.payment_settlement import CHANNEL_PSP, idem_key_psp
from app.services.users import clear_settings_cache, set_setting

PAY_URL = "https://pay.sunpay.test"
API_KEY = "sunpay-test-key-abcdef"
TOKEN = "tok-page"
AMOUNT = 50_000
FAILED_TITLE = "تأیید پرداخت ناموفق"


class PspReturnPageStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app.config import get_settings

        get_settings.cache_clear()
        clear_settings_cache()
        await close_client()
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 't.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        async with self.Session() as session:
            await set_setting(session, "pay_psp_enabled", "1")
            await set_setting(session, "psp_provider", "sunpay")
            await set_setting(session, "psp_merchant_id", API_KEY)
            await set_setting(session, "psp_pay_url", PAY_URL)
            user = BotUser(telegram_id=5001, username="u", full_name="u", referral_code="r6", role=Role.USER.value)
            session.add(user)
            await session.flush()
            payment = Payment(
                user_id=user.id,
                amount=AMOUNT,
                method=PaymentMethod.PSP.value,
                status=PaymentStatus.PENDING.value,
                is_wallet_topup=True,
            )
            session.add(payment)
            await session.flush()
            settlement = PaymentSettlement(
                payment_id=int(payment.id),
                tenant_key=0,
                channel=CHANNEL_PSP,
                provider="sunpay",
                status=SettlementStatus.AWAITING.value,
                amount=AMOUNT,
                currency="IRT",
                idempotency_key=idem_key_psp(int(payment.id)),
                external_ref=TOKEN,
                checkout_url=f"{PAY_URL}/pay/{TOKEN}",
            )
            session.add(settlement)
            await session.commit()
            self.sid = int(settlement.id)
        self.gateway_calls = 0
        self.gateway_error: Exception | None = None

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_settlement_pages(app, get_db=get_db)
        self.web = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        from app.config import get_settings

        await self.web.aclose()
        await close_client()
        await self.engine.dispose()
        self._tmp.cleanup()
        clear_settings_cache()
        get_settings.cache_clear()

    def _handler(self, _request: httpx.Request) -> httpx.Response:
        self.gateway_calls += 1
        if self.gateway_error is not None:
            raise self.gateway_error
        return httpx.Response(200, json={"success": 0, "error": {"code": "NOT_PAID", "message": "-"}})

    async def _return(self, **params: str) -> httpx.Response:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self._handler))
        try:
            with (
                patch.object(sunpay_mod, "_http", return_value=client),
                patch.object(sunpay_mod, "_resolve_host_addresses", return_value=["8.8.8.8"]),
                patch("app.services.payment_settlement._public_base", return_value="https://bot.test"),
            ):
                return await self.web.get(f"/payments/settlement/psp/sunpay/return/{self.sid}", params=params)
        finally:
            await client.aclose()

    async def _status(self) -> str:
        async with self.Session() as session:
            row = await session.get(PaymentSettlement, self.sid)
            assert row is not None
            return row.status

    async def test_cancel_at_the_bank_is_not_a_failed_payment(self) -> None:
        resp = await self._return(success="0", token=TOKEN, invoice_id=str(self.sid))

        self.assertEqual(resp.status_code, 200)
        self.assertIn("پرداخت انجام نشد", resp.text)
        self.assertNotIn(FAILED_TITLE, resp.text)
        self.assertEqual(self.gateway_calls, 0)
        self.assertEqual(await self._status(), SettlementStatus.AWAITING.value)

    async def test_payment_not_reported_yet_shows_pending(self) -> None:
        resp = await self._return(success="1", token=TOKEN, invoice_id=str(self.sid))

        self.assertEqual(resp.status_code, 200)
        self.assertIn("پرداخت هنوز تأیید نشده", resp.text)
        self.assertNotIn(FAILED_TITLE, resp.text)
        self.assertEqual(self.gateway_calls, 1)

    async def test_gateway_timeout_shows_pending(self) -> None:
        self.gateway_error = httpx.ConnectTimeout("timeout")

        resp = await self._return(success="1", token=TOKEN, invoice_id=str(self.sid))

        self.assertEqual(resp.status_code, 200)
        self.assertIn("پرداخت هنوز تأیید نشده", resp.text)
        self.assertEqual(await self._status(), SettlementStatus.AWAITING.value)

    async def test_closed_settlement_still_shows_failure(self) -> None:
        async with self.Session() as session:
            row = await session.get(PaymentSettlement, self.sid)
            assert row is not None
            row.status = SettlementStatus.FAILED.value
            await session.commit()

        resp = await self._return(success="1", token=TOKEN, invoice_id=str(self.sid))

        self.assertEqual(resp.status_code, 400)
        self.assertIn(FAILED_TITLE, resp.text)


if __name__ == "__main__":
    unittest.main()
