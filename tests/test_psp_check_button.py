"""«بررسی پرداخت»: only the payer re-verifies their own gateway payment, delivered once."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot.handlers import psp_check
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
from app.services import payment_settlement
from app.services.payment_providers import sunpay as sunpay_mod
from app.services.payment_providers.sunpay import close_client
from app.services.payment_settlement import (
    CHANNEL_PSP,
    PspCheckOutcome,
    check_psp_payment,
    idem_key_psp,
)
from app.services.users import clear_settings_cache, set_setting

PAY_URL = "https://pay.sunpay.test"
API_KEY = "sunpay-test-key-abcdef"
TOKEN = "tok-check"
AMOUNT = 50_000


class CheckPspPaymentTests(unittest.IsolatedAsyncioTestCase):
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
            payer = BotUser(telegram_id=4001, username="p", full_name="p", referral_code="r4", role=Role.USER.value)
            other = BotUser(telegram_id=4002, username="o", full_name="o", referral_code="r5", role=Role.USER.value)
            session.add_all([payer, other])
            await session.flush()
            payment = Payment(
                user_id=payer.id,
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
            self.payer_id = int(payer.id)
            self.other_id = int(other.id)
            self.settlement_id = int(settlement.id)
        self.paid = True
        self.verify_calls = 0
        self.notify = AsyncMock()
        self.approve = AsyncMock(return_value=None)

    async def asyncTearDown(self) -> None:
        from app.config import get_settings

        await close_client()
        await self.engine.dispose()
        self._tmp.cleanup()
        clear_settings_cache()
        get_settings.cache_clear()

    def _handler(self, _request: httpx.Request) -> httpx.Response:
        self.verify_calls += 1
        if not self.paid:
            return httpx.Response(200, json={"success": 0, "error": {"code": "NOT_PAID", "message": "-"}})
        return httpx.Response(
            200,
            json={
                "success": 1,
                "data": {
                    "amount": AMOUNT,
                    "invoice_id": self.settlement_id,
                    "reference": "REF-1",
                    "paid_at": "2026-09-27 09:30:00",
                },
            },
        )

    async def _check(self, *, user_id: int | None = None, shop_id: int | None = None) -> PspCheckOutcome:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self._handler))
        try:
            with (
                patch.object(sunpay_mod, "_http", return_value=client),
                patch.object(sunpay_mod, "_resolve_host_addresses", return_value=["8.8.8.8"]),
                patch("app.services.payment_settlement._public_base", return_value="https://bot.test"),
                patch("app.services.payment_settlement.approve_payment", self.approve),
                patch("app.services.payment_settlement.notify_after_settlement", self.notify),
            ):
                async with self.Session() as session:
                    return await check_psp_payment(
                        session,
                        settlement_id=self.settlement_id,
                        user_id=self.payer_id if user_id is None else user_id,
                        shop_id=shop_id,
                    )
        finally:
            await client.aclose()

    async def _status(self) -> str:
        async with self.Session() as session:
            row = await session.get(PaymentSettlement, self.settlement_id)
            assert row is not None
            return row.status

    async def test_paid_payment_settles_and_is_delivered_once(self) -> None:
        self.assertEqual(await self._check(), PspCheckOutcome.SETTLED)
        self.assertEqual(await self._check(), PspCheckOutcome.SETTLED)

        self.assertEqual(await self._status(), SettlementStatus.SETTLED.value)
        self.approve.assert_awaited_once()
        self.notify.assert_awaited_once()
        self.assertEqual(self.verify_calls, 1)

    async def test_unpaid_payment_stays_open(self) -> None:
        self.paid = False
        self.assertEqual(await self._check(), PspCheckOutcome.PENDING)
        self.assertEqual(await self._status(), SettlementStatus.AWAITING.value)
        self.approve.assert_not_awaited()

    async def test_another_customer_cannot_check_the_payment(self) -> None:
        with self.assertRaises(ValueError):
            await self._check(user_id=self.other_id)
        self.assertEqual(self.verify_calls, 0)
        self.assertEqual(await self._status(), SettlementStatus.AWAITING.value)

    async def test_another_shop_cannot_check_the_payment(self) -> None:
        with self.assertRaises(ValueError):
            await self._check(shop_id=self.other_id)
        self.assertEqual(self.verify_calls, 0)

    async def test_payment_in_review_is_reported_without_calling_the_gateway(self) -> None:
        async with self.Session() as session:
            row = await session.get(PaymentSettlement, self.settlement_id)
            assert row is not None
            row.status = SettlementStatus.FAILED.value
            row.error_message = "review:payment_not_pending"
            await session.commit()

        self.assertEqual(await self._check(), PspCheckOutcome.REVIEW)
        self.assertEqual(self.verify_calls, 0)


class PspCheckHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        payment_settlement._recent_psp_checks.clear()

    def _callback(self, settlement_id: int) -> MagicMock:
        callback = MagicMock()
        callback.data = f"{psp_check.CHECK_PREFIX}{settlement_id}"
        callback.answer = AsyncMock()
        callback.message = MagicMock()
        callback.message.answer = AsyncMock()
        callback.message.edit_reply_markup = AsyncMock()
        return callback

    async def test_tap_is_answered_before_the_gateway_check(self) -> None:
        order: list[str] = []
        callback = self._callback(7)
        callback.answer.side_effect = lambda *a, **k: order.append("answer")

        async def check(*_a, **_k):
            order.append("check")
            return PspCheckOutcome.SETTLED

        with patch.object(psp_check, "check_psp_payment", side_effect=check):
            await psp_check.psp_check_cb(callback, MagicMock(), SimpleNamespace(id=1))

        self.assertEqual(order, ["answer", "check"])
        callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
        self.assertEqual(
            callback.message.answer.await_args.args[0], psp_check.OUTCOME_TEXT[PspCheckOutcome.SETTLED]
        )

    async def test_repeated_taps_within_window_check_once(self) -> None:
        check = AsyncMock(return_value=PspCheckOutcome.PENDING)
        with patch.object(psp_check, "check_psp_payment", check):
            await psp_check.psp_check_cb(self._callback(8), MagicMock(), SimpleNamespace(id=1))
            await psp_check.psp_check_cb(self._callback(8), MagicMock(), SimpleNamespace(id=1))
        check.assert_awaited_once()

    def test_button_callback_data_fits_telegram_limit(self) -> None:
        button = psp_check.psp_check_button(2**31 - 1)
        self.assertLessEqual(len(button.callback_data.encode()), 64)


if __name__ == "__main__":
    unittest.main()
