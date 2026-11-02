"""Paid gateway payments that cannot be applied go to staff review instead of looping."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.settlement_pages import register_settlement_pages
from app.db import Base
from app.db.models import (
    BotUser,
    Order,
    OrderStatus,
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
    complete_psp_return,
    create_psp_checkout,
    idem_key_psp,
    poll_psp_settlements,
)
from app.services.psp_settlement_review import REVIEW_IN_PROGRESS_MESSAGE, REVIEW_PREFIX
from app.services.users import clear_settings_cache, set_setting

PAY_URL = "https://pay.sunpay.test"
API_KEY = "sunpay-test-key-abcdef"
TOKEN = "tok-review"
AMOUNT = 50_000


class PspSettlementReviewTests(unittest.IsolatedAsyncioTestCase):
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
            user = BotUser(
                telegram_id=3001,
                username="buyer",
                full_name="buyer",
                referral_code="r3",
                role=Role.USER.value,
            )
            session.add(user)
            await session.commit()
            self.user_id = int(user.id)
        self.verify_calls = 0
        self.paid_amount = AMOUNT
        self.notify = AsyncMock(return_value=1)
        self.approve = AsyncMock(return_value=None)

    async def asyncTearDown(self) -> None:
        from app.config import get_settings

        await close_client()
        await self.engine.dispose()
        self._tmp.cleanup()
        clear_settings_cache()
        get_settings.cache_clear()

    async def _seed(
        self,
        *,
        payment_status: str = PaymentStatus.PENDING.value,
        order_status: str | None = OrderStatus.AWAITING_RECEIPT.value,
    ) -> tuple[int, int]:
        async with self.Session() as session:
            order_id = None
            if order_status is not None:
                order = Order(user_id=self.user_id, amount=AMOUNT, status=order_status)
                session.add(order)
                await session.flush()
                order_id = int(order.id)
            payment = Payment(
                user_id=self.user_id,
                order_id=order_id,
                amount=AMOUNT,
                method=PaymentMethod.PSP.value,
                status=payment_status,
                is_wallet_topup=order_id is None,
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
                provider_payload=json.dumps({"success": 1, "data": {"token": TOKEN}}),
            )
            session.add(settlement)
            await session.commit()
            self.settlement_id = int(settlement.id)
            return int(payment.id), int(settlement.id)

    def _handler(self, _request: httpx.Request) -> httpx.Response:
        self.verify_calls += 1
        return httpx.Response(
            200,
            json={
                "success": 1,
                "data": {
                    "amount": self.paid_amount,
                    "invoice_id": self.settlement_id,
                    "reference": "REF-<b>9</b>",
                    "paid_at": "2026-09-27 09:30:00",
                    "detail": {},
                },
            },
        )

    def _patches(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(self._handler))
        return client, (
            patch.object(sunpay_mod, "_http", return_value=client),
            patch.object(sunpay_mod, "_resolve_host_addresses", return_value=["8.8.8.8"]),
            patch("app.services.payment_settlement._public_base", return_value="https://bot.test"),
            patch("app.services.payment_settlement.approve_payment", self.approve),
            patch("app.services.notifications.notify_settlement_review", self.notify),
            patch("app.services.payment_settlement.notify_after_settlement", AsyncMock()),
        )

    async def _return(self, settlement_id: int) -> PaymentSettlement:
        client, patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            async with self.Session() as session:
                result = await complete_psp_return(
                    session,
                    provider="sunpay",
                    settlement_id=settlement_id,
                    callback_params={"success": "1", "token": TOKEN, "invoice_id": str(settlement_id)},
                    reseller_id=None,
                )
        await client.aclose()
        return result

    async def _poll(self) -> None:
        client, patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            async with self.Session() as session:
                await poll_psp_settlements(session)
        await client.aclose()

    async def _row(self, settlement_id: int) -> PaymentSettlement:
        async with self.Session() as session:
            row = await session.get(PaymentSettlement, settlement_id)
            assert row is not None
            return row

    async def test_paid_link_of_replaced_payment_goes_to_review_once(self) -> None:
        _, sid = await self._seed(payment_status=PaymentStatus.REJECTED.value)

        await self._return(sid)
        await self._return(sid)
        await self._poll()

        row = await self._row(sid)
        self.assertEqual(row.status, SettlementStatus.FAILED.value)
        self.assertEqual(row.error_message, f"{REVIEW_PREFIX}payment_not_pending")
        self.assertEqual(json.loads(row.provider_payload)["verify"]["reference"], "REF-<b>9</b>")
        self.approve.assert_not_awaited()
        self.notify.assert_awaited_once()
        self.assertIsNone(self.notify.await_args.kwargs["shop_id"])
        reason = self.notify.await_args.kwargs["reason"]
        self.assertIn("REF-&lt;b&gt;9&lt;/b&gt;", reason)
        self.assertNotIn("<b>", reason)
        self.assertEqual(self.verify_calls, 1)

    async def test_paid_link_of_already_paid_order_goes_to_review(self) -> None:
        _, sid = await self._seed(order_status=OrderStatus.PAID.value)

        await self._return(sid)

        row = await self._row(sid)
        self.assertEqual(row.error_message, f"{REVIEW_PREFIX}order_closed")
        self.approve.assert_not_awaited()
        self.notify.assert_awaited_once()

    async def test_different_paid_amount_goes_to_review(self) -> None:
        _, sid = await self._seed()
        self.paid_amount = AMOUNT - 1000

        await self._return(sid)

        row = await self._row(sid)
        self.assertEqual(row.error_message, f"{REVIEW_PREFIX}amount_mismatch")
        self.approve.assert_not_awaited()
        self.assertIn(str(AMOUNT - 1000), self.notify.await_args.kwargs["reason"])

    async def test_settled_payment_keeps_gateway_reference(self) -> None:
        _, sid = await self._seed(order_status=None)

        await self._return(sid)

        row = await self._row(sid)
        self.assertEqual(row.status, SettlementStatus.SETTLED.value)
        verify = json.loads(row.provider_payload)["verify"]
        self.assertEqual(verify["reference"], "REF-<b>9</b>")
        self.assertEqual(verify["paid_at"], "2026-09-27 09:30:00")
        self.approve.assert_awaited_once()
        self.notify.assert_not_awaited()

    async def test_new_checkout_does_not_overwrite_a_payment_in_review(self) -> None:
        payment_id, sid = await self._seed()
        self.paid_amount = AMOUNT - 1000
        await self._return(sid)

        with patch("app.services.public_https.https_public_base", return_value="https://bot.test"):
            async with self.Session() as session:
                payment = await session.get(Payment, payment_id)
                assert payment is not None
                with self.assertRaisesRegex(ValueError, REVIEW_IN_PROGRESS_MESSAGE):
                    await create_psp_checkout(session, payment)

        row = await self._row(sid)
        self.assertEqual(row.error_message, f"{REVIEW_PREFIX}amount_mismatch")

    async def test_return_page_says_payment_is_in_review(self) -> None:
        _, sid = await self._seed(payment_status=PaymentStatus.REJECTED.value)

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_settlement_pages(app, get_db=get_db)
        client, patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as web:
                resp = await web.get(
                    f"/payments/settlement/psp/sunpay/return/{sid}",
                    params={"success": "1", "token": TOKEN, "invoice_id": str(sid)},
                )
        await client.aclose()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("پرداخت در حال بررسی است", resp.text)


if __name__ == "__main__":
    unittest.main()
