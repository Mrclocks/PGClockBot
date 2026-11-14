"""Approving a top-up must credit the wallet once, even when two resumes race past the ledger check."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import (
    Base,
    BotUser,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Role,
    WalletTransaction,
)
from app.services import orders
from app.services.wallet import credit_wallet

TOPUP_AMOUNT = 50_000


class TopupCreditIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as session:
            user = BotUser(telegram_id=9001, referral_code="R9001", role=Role.USER.value)
            session.add(user)
            await session.flush()
            payment = Payment(
                user_id=user.id, amount=TOPUP_AMOUNT, method=PaymentMethod.CARD.value, is_wallet_topup=True,
                status=PaymentStatus.APPROVED.value, reviewed_by=1,
            )
            session.add(payment)
            await session.commit()
            self.user_id, self.payment_id = int(user.id), int(payment.id)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _balance_and_rows(self) -> tuple[int, int]:
        async with self.sessions() as session:
            balance = await session.scalar(select(BotUser.wallet_balance).where(BotUser.id == self.user_id))
            rows = await session.scalar(
                select(func.count()).select_from(WalletTransaction).where(WalletTransaction.user_id == self.user_id)
            )
        return int(balance or 0), int(rows or 0)

    async def _resume(self) -> None:
        async with self.sessions() as session:
            payment = await session.get(Payment, self.payment_id)
            await orders._resume_approved_payment(session, payment)

    async def test_two_resumes_that_both_miss_the_ledger_credit_once(self) -> None:
        # Both callers read "not credited yet" before either writes: the race the SELECT cannot close.
        with patch.object(orders, "_wallet_topup_already_credited", AsyncMock(return_value=False)):
            await self._resume()
            await self._resume()
        self.assertEqual(await self._balance_and_rows(), (TOPUP_AMOUNT, 1))

    async def test_a_credit_recorded_without_a_key_is_still_recognised(self) -> None:
        async with self.sessions() as session:
            user = await session.get(BotUser, self.user_id)
            await credit_wallet(session, user, TOPUP_AMOUNT, orders._wallet_topup_credit_reason(self.payment_id))
        await self._resume()
        self.assertEqual(await self._balance_and_rows(), (TOPUP_AMOUNT, 1))


if __name__ == "__main__":
    unittest.main()
