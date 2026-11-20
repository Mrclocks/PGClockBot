"""A receipt whose fingerprint cannot be stored is still attached and still reaches review."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Base, BotUser, Order, Payment, PaymentReceiptFingerprint, PaymentStatus, Plan
from app.services.receipts import submit_receipt
from tests.postgres_support import scoped_async_engine


class FingerprintFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([BotUser(id=1, telegram_id=1001, referral_code="c1"), Plan(id=1, name="p", price=100_000)])
            await session.flush()
            session.add(Order(id=1, user_id=1, plan_id=1, amount=100_000, status="awaiting_receipt"))
            await session.flush()
            session.add(Payment(id=1, user_id=1, order_id=1, amount=100_000, method="card", status=PaymentStatus.PENDING.value))
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_a_failed_fingerprint_flush_does_not_break_the_review(self) -> None:
        async def broken_store(session, **_kwargs):
            # A failed flush leaves the session refusing every query until it is rolled back.
            session.add(PaymentReceiptFingerprint(payment_id=999, sha256="x"))
            await session.flush()

        async def review(session, payment, **_kwargs):
            found = await session.scalar(select(Payment.receipt_file_id).where(Payment.id == payment.id))
            return f"reviewing {found}"

        with patch("app.services.receipts.download_receipt_sha256", AsyncMock(return_value="ab" * 32)), \
                patch("app.services.receipts.store_receipt_fingerprint", broken_store), \
                patch("app.services.receipts.process_receipt", review):
            async with self.Session() as session:
                result = await submit_receipt(session, await session.get(Payment, 1), bot=AsyncMock(), file_id="FILE")
        self.assertEqual(result, "reviewing FILE")
        async with self.Session() as session:
            self.assertEqual((await session.get(Payment, 1)).receipt_file_id, "FILE")


if __name__ == "__main__":
    unittest.main()
