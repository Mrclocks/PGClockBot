"""«بزن به کارت» poll: expired or abandoned orders back off and cannot starve a new payment."""

from __future__ import annotations

import json
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
from app.db.models import BotUser, Payment, PaymentMethod, PaymentSettlement, PaymentStatus, Role, SettlementStatus
from app.services import bbc_settlement as flow
from app.services.payment_providers import bezanbecard as bbc
from app.services.payment_settlement import CHANNEL_BBC, idem_key_bbc
from app.services.users import clear_settings_cache, set_setting

KEY = "platform-key-123456"
AMOUNT = 50_000
UNIQUE = 500_043
PAID_TOKEN = "tok_paid_0001"


class BbcPollFairnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'poll.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        async with self.Session() as session:
            session.add(BotUser(id=1, telegram_id=101, referral_code="c1", role=Role.USER.value))
            await session.commit()
            await set_setting(session, "pay_bbc_enabled", "1")
            await set_setting(session, "bbc_api_key", KEY)
        clear_settings_cache()
        self.read_tokens: list[str] = []
        self.payment_ids: dict[str, int] = {}
        self.pending_tokens: set[str] = set()

    async def asyncTearDown(self) -> None:
        clear_settings_cache()
        await self.engine.dispose()
        self._tmp.cleanup()

    async def _seed(self, token: str, *, status: str, created_ago: timedelta, updated_ago: timedelta) -> int:
        async with self.Session() as session:
            payment = Payment(
                user_id=1,
                amount=AMOUNT,
                method=PaymentMethod.BBC.value,
                status=PaymentStatus.PENDING.value,
                is_wallet_topup=True,
            )
            session.add(payment)
            await session.flush()
            now = datetime.now(timezone.utc)
            settlement = PaymentSettlement(
                payment_id=int(payment.id),
                tenant_key=0,
                channel=CHANNEL_BBC,
                provider="bezanbecard",
                status=status,
                amount=AMOUNT,
                currency="IRT",
                idempotency_key=idem_key_bbc(int(payment.id)),
                external_ref=token,
                provider_payload=json.dumps({"order": {"unique_amount_rial": UNIQUE}}),
                created_at=now - created_ago,
            )
            session.add(settlement)
            await session.flush()
            await session.execute(
                update(PaymentSettlement)
                .where(PaymentSettlement.id == settlement.id)
                .values(updated_at=now - updated_ago)
            )
            await session.commit()
            self.payment_ids[token] = int(payment.id)
            return int(settlement.id)

    async def _get_order(self, *, api_key: str, payment_token: str) -> bbc.BbcOrder:
        self.read_tokens.append(payment_token)
        if payment_token == PAID_TOKEN:
            status = "paid"
        elif payment_token in self.pending_tokens:
            status = "pending"
        else:
            status = "expired"
        return bbc.BbcOrder(
            order_id=f"ord_{payment_token}",
            payment_token=payment_token,
            status=status,
            unique_amount_rial=UNIQUE,
            base_amount_rial=AMOUNT * 10,
            merchant_order_id=str(self.payment_ids[payment_token]),
        )

    async def _poll(self) -> None:
        with (
            patch.object(bbc, "get_order", side_effect=self._get_order),
            patch("app.services.payment_settlement.approve_payment", AsyncMock(return_value=None)),
            patch.object(flow, "notify_after_settlement", AsyncMock()),
        ):
            async with self.Session() as session:
                await flow.poll_bbc_settlements(session)

    async def _row(self, settlement_id: int) -> PaymentSettlement:
        async with self.Session() as session:
            row = await session.get(PaymentSettlement, settlement_id)
            assert row is not None
            return row

    async def test_new_payment_settles_despite_many_expired_orders(self) -> None:
        expired = flow.POLL_BATCH + 20
        for i in range(expired):
            await self._seed(
                f"tok_expired_{i:04d}",
                status=SettlementStatus.EXPIRED.value,
                created_ago=timedelta(hours=2),
                updated_ago=timedelta(hours=1),
            )
        await self._poll()
        await self._poll()
        self.assertEqual(len(set(self.read_tokens)), expired)

        paid_id = await self._seed(
            PAID_TOKEN,
            status=SettlementStatus.AWAITING.value,
            created_ago=timedelta(0),
            updated_ago=timedelta(0),
        )
        for _ in range(3):
            await self._poll()

        self.assertEqual((await self._row(paid_id)).status, SettlementStatus.SETTLED.value)

    async def test_read_expired_order_waits_and_keeps_its_expiry_time(self) -> None:
        sid = await self._seed(
            "tok_expired_one",
            status=SettlementStatus.EXPIRED.value,
            created_ago=timedelta(hours=2),
            updated_ago=timedelta(hours=1),
        )
        before = (await self._row(sid)).updated_at

        await self._poll()
        await self._poll()

        self.assertEqual(self.read_tokens, ["tok_expired_one"])
        row = await self._row(sid)
        self.assertEqual(row.updated_at, before)
        self.assertIsNotNone(row.next_check_at)

    async def test_fresh_open_order_is_read_every_run(self) -> None:
        self.pending_tokens.add("tok_fresh_0001")
        await self._seed(
            "tok_fresh_0001",
            status=SettlementStatus.AWAITING.value,
            created_ago=timedelta(minutes=1),
            updated_ago=timedelta(minutes=1),
        )

        await self._poll()
        await self._poll()

        self.assertEqual(self.read_tokens, ["tok_fresh_0001", "tok_fresh_0001"])

if __name__ == "__main__":
    unittest.main()
