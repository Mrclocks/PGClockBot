"""Picking a payment method again must not void a gateway link the customer can still pay."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
from app.services.open_psp_checkouts import PSP_CHECKOUT_REUSE_WINDOW
from app.services.orders import start_method_payment
from app.services.payment_settlement import CHANNEL_PSP, idem_key_psp, settle_and_approve

AMOUNT = 50_000


class OpenPspCheckoutPaymentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 't.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        async with self.Session() as session:
            user = BotUser(
                telegram_id=2001,
                username="buyer",
                full_name="buyer",
                referral_code="r2",
                role=Role.USER.value,
            )
            session.add(user)
            await session.flush()
            order = Order(user_id=user.id, amount=AMOUNT, status=OrderStatus.PENDING.value)
            session.add(order)
            await session.commit()
            self.user_id = int(user.id)
            self.order_id = int(order.id)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def _pick(self, method: str) -> int:
        async with self.Session() as session:
            order = await session.get(Order, self.order_id)
            payment = await start_method_payment(session, order, self.user_id, method)
            return int(payment.id)

    async def _open_checkout(
        self, payment_id: int, *, age: timedelta = timedelta(0), status: str = SettlementStatus.AWAITING.value
    ) -> int:
        async with self.Session() as session:
            created = datetime.now(timezone.utc) - age
            settlement = PaymentSettlement(
                payment_id=payment_id,
                tenant_key=0,
                channel=CHANNEL_PSP,
                provider="sunpay",
                status=status,
                amount=AMOUNT,
                currency="IRT",
                idempotency_key=idem_key_psp(payment_id),
                external_ref=f"token-{payment_id}",
                checkout_url=f"https://pay.sunpay.test/pay/token-{payment_id}",
                created_at=created,
                updated_at=created,
            )
            session.add(settlement)
            await session.commit()
            return int(settlement.id)

    async def _payment_status(self, payment_id: int) -> str:
        async with self.Session() as session:
            payment = await session.get(Payment, payment_id)
            assert payment is not None
            return payment.status

    async def _payment_count(self) -> int:
        async with self.Session() as session:
            return int(
                await session.scalar(
                    select(func.count()).select_from(Payment).where(Payment.order_id == self.order_id)
                )
            )

    async def test_repicking_gateway_soon_shows_the_same_payment(self) -> None:
        first = await self._pick(PaymentMethod.PSP.value)
        await self._open_checkout(first)

        again = await self._pick(PaymentMethod.PSP.value)

        self.assertEqual(again, first)
        self.assertEqual(await self._payment_count(), 1)
        self.assertEqual(await self._payment_status(first), PaymentStatus.PENDING.value)

    async def test_old_gateway_link_stays_payable_after_a_new_one(self) -> None:
        first = await self._pick(PaymentMethod.PSP.value)
        await self._open_checkout(first, age=PSP_CHECKOUT_REUSE_WINDOW + timedelta(minutes=5))

        second = await self._pick(PaymentMethod.PSP.value)

        self.assertNotEqual(second, first)
        self.assertEqual(await self._payment_status(first), PaymentStatus.PENDING.value)
        self.assertEqual(await self._payment_status(second), PaymentStatus.PENDING.value)

    async def test_switching_method_keeps_open_gateway_payment_and_rejects_others(self) -> None:
        card = await self._pick(PaymentMethod.CARD.value)
        gateway = await self._pick(PaymentMethod.PSP.value)
        await self._open_checkout(gateway)

        await self._pick(PaymentMethod.CARD.value)

        self.assertEqual(await self._payment_status(card), PaymentStatus.REJECTED.value)
        self.assertEqual(await self._payment_status(gateway), PaymentStatus.PENDING.value)

    async def test_gateway_payment_without_open_link_is_still_replaced(self) -> None:
        first = await self._pick(PaymentMethod.PSP.value)
        await self._open_checkout(first, status=SettlementStatus.FAILED.value)

        second = await self._pick(PaymentMethod.PSP.value)

        self.assertNotEqual(second, first)
        self.assertEqual(await self._payment_status(first), PaymentStatus.REJECTED.value)

    async def test_paying_the_older_link_completes_the_order(self) -> None:
        first = await self._pick(PaymentMethod.PSP.value)
        settlement_id = await self._open_checkout(
            first, age=PSP_CHECKOUT_REUSE_WINDOW + timedelta(minutes=5)
        )
        second = await self._pick(PaymentMethod.PSP.value)

        with patch("app.services.orders.fulfill_paid_order", AsyncMock(return_value=None)):
            async with self.Session() as session:
                settlement = await session.get(PaymentSettlement, settlement_id)
                assert settlement is not None
                await settle_and_approve(
                    session, settlement, expected_amount=AMOUNT, external_ref=f"token-{first}"
                )

        async with self.Session() as session:
            order = await session.get(Order, self.order_id)
            settlement = await session.get(PaymentSettlement, settlement_id)
            assert order is not None and settlement is not None
            self.assertEqual(order.status, OrderStatus.PAID.value)
            self.assertEqual(settlement.status, SettlementStatus.SETTLED.value)
        self.assertEqual(await self._payment_status(first), PaymentStatus.APPROVED.value)
        self.assertEqual(await self._payment_status(second), PaymentStatus.REJECTED.value)


if __name__ == "__main__":
    unittest.main()
