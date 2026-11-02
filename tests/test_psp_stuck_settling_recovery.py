"""A gateway settlement left in SETTLING by a crash is finished or reopened, once."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import update
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
from app.services.payment_settlement import (
    CHANNEL_PSP,
    PSP_SETTLING_STALE_AFTER,
    idem_key_psp,
    recover_stuck_psp_settlements,
)

AMOUNT = 50_000


class StuckSettlingRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 't.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        async with self.Session() as session:
            user = BotUser(telegram_id=8001, username="u", full_name="u", referral_code="r9", role=Role.USER.value)
            session.add(user)
            await session.commit()
            self.user_id = int(user.id)
        self.notify = AsyncMock()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def _seed(self, *, payment_status: str, claimed_ago: timedelta) -> int:
        async with self.Session() as session:
            payment = Payment(
                user_id=self.user_id,
                amount=AMOUNT,
                method=PaymentMethod.PSP.value,
                status=payment_status,
                is_wallet_topup=True,
            )
            session.add(payment)
            await session.flush()
            settlement = PaymentSettlement(
                payment_id=int(payment.id),
                tenant_key=0,
                channel=CHANNEL_PSP,
                provider="sunpay",
                status=SettlementStatus.SETTLING.value,
                amount=AMOUNT,
                currency="IRT",
                idempotency_key=idem_key_psp(int(payment.id)),
                external_ref=f"tok-{payment.id}",
            )
            session.add(settlement)
            await session.flush()
            # The claim time is updated_at; set it explicitly after the insert.
            await session.execute(
                update(PaymentSettlement)
                .where(PaymentSettlement.id == settlement.id)
                .values(updated_at=datetime.now(timezone.utc) - claimed_ago)
            )
            await session.commit()
            return int(settlement.id)

    async def _recover(self) -> int:
        with patch("app.services.payment_settlement.notify_after_settlement", self.notify):
            async with self.Session() as session:
                return await recover_stuck_psp_settlements(session)

    async def _status(self, settlement_id: int) -> str:
        async with self.Session() as session:
            row = await session.get(PaymentSettlement, settlement_id)
            assert row is not None
            return row.status

    async def test_approved_payment_is_finished_and_notified_once(self) -> None:
        sid = await self._seed(
            payment_status=PaymentStatus.APPROVED.value,
            claimed_ago=PSP_SETTLING_STALE_AFTER + timedelta(minutes=1),
        )

        self.assertEqual(await self._recover(), 1)
        self.assertEqual(await self._recover(), 0)

        self.assertEqual(await self._status(sid), SettlementStatus.SETTLED.value)
        self.notify.assert_awaited_once()

    async def test_unapproved_claim_is_reopened_for_verify(self) -> None:
        sid = await self._seed(
            payment_status=PaymentStatus.PENDING.value,
            claimed_ago=PSP_SETTLING_STALE_AFTER + timedelta(minutes=1),
        )

        self.assertEqual(await self._recover(), 0)

        self.assertEqual(await self._status(sid), SettlementStatus.AWAITING.value)
        self.notify.assert_not_awaited()

    async def test_recent_claim_is_left_to_the_settle_in_progress(self) -> None:
        sid = await self._seed(payment_status=PaymentStatus.APPROVED.value, claimed_ago=timedelta(minutes=1))

        self.assertEqual(await self._recover(), 0)

        self.assertEqual(await self._status(sid), SettlementStatus.SETTLING.value)
        self.notify.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
