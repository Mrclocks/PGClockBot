"""Turning the gateway off stops new checkouts but still confirms ones already paid."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
from app.services.payment_settlement import (
    CHANNEL_PSP,
    create_psp_checkout,
    idem_key_psp,
    poll_psp_settlements,
)
from app.services.users import clear_settings_cache, set_setting

PAY_URL = "https://pay.sunpay.test"
API_KEY = "sunpay-test-key-abcdef"
TOKEN = "tok-disabled"
AMOUNT = 50_000


class VerifyAfterDisableTests(unittest.IsolatedAsyncioTestCase):
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
            await set_setting(session, "pay_psp_enabled", "0")
            await set_setting(session, "psp_provider", "sunpay")
            await set_setting(session, "psp_merchant_id", API_KEY)
            await set_setting(session, "psp_pay_url", PAY_URL)
            user = BotUser(telegram_id=6001, username="u", full_name="u", referral_code="r7", role=Role.USER.value)
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
            self.payment_id = int(payment.id)
            self.sid = int(settlement.id)

    async def asyncTearDown(self) -> None:
        from app.config import get_settings

        await close_client()
        await self.engine.dispose()
        self._tmp.cleanup()
        clear_settings_cache()
        get_settings.cache_clear()

    def _handler(self, _request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": 1,
                "data": {
                    "amount": AMOUNT,
                    "invoice_id": self.sid,
                    "reference": "REF-1",
                    "paid_at": "2026-09-27 09:30:00",
                },
            },
        )

    async def test_paid_open_checkout_settles_after_gateway_is_turned_off(self) -> None:
        approve = AsyncMock(return_value=None)
        client = httpx.AsyncClient(transport=httpx.MockTransport(self._handler))
        with (
            patch.object(sunpay_mod, "_http", return_value=client),
            patch.object(sunpay_mod, "_resolve_host_addresses", return_value=["8.8.8.8"]),
            patch("app.services.payment_settlement._public_base", return_value="https://bot.test"),
            patch("app.services.payment_settlement.approve_payment", approve),
            patch("app.services.payment_settlement.notify_after_settlement", AsyncMock()),
        ):
            async with self.Session() as session:
                settled = await poll_psp_settlements(session)
        await client.aclose()

        self.assertEqual(settled, 1)
        approve.assert_awaited_once()
        async with self.Session() as session:
            row = await session.get(PaymentSettlement, self.sid)
            assert row is not None
            self.assertEqual(row.status, SettlementStatus.SETTLED.value)

    async def test_turned_off_gateway_refuses_new_checkouts(self) -> None:
        async with self.Session() as session:
            payment = Payment(
                user_id=(await session.get(Payment, self.payment_id)).user_id,
                amount=AMOUNT,
                method=PaymentMethod.PSP.value,
                status=PaymentStatus.PENDING.value,
                is_wallet_topup=True,
            )
            session.add(payment)
            await session.commit()
            with self.assertRaisesRegex(ValueError, "غیرفعال"):
                await create_psp_checkout(session, payment)


if __name__ == "__main__":
    unittest.main()
