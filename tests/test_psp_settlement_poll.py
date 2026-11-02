"""PSP poller fairness: abandoned checkouts back off and cannot starve new payments."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
import sqlalchemy as sa
from sqlalchemy import select, text
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
    PSP_POLL_BACKOFF_MAX,
    PSP_POLL_BATCH,
    idem_key_psp,
    poll_psp_settlements,
    psp_poll_delay,
)
from app.services.users import clear_settings_cache, set_setting

ROOT = Path(__file__).resolve().parents[1]
PAY_URL = "https://pay.sunpay.test"
API_KEY = "sunpay-test-key-abcdef"
AMOUNT = 50_000
PAID_TOKEN = "paid-token"


class PollDelayTests(unittest.TestCase):
    def test_fresh_checkouts_are_due_every_run(self) -> None:
        self.assertEqual(psp_poll_delay(timedelta(minutes=1)), timedelta(0))

    def test_delay_grows_with_age_and_is_capped(self) -> None:
        delays = [
            psp_poll_delay(age)
            for age in (
                timedelta(minutes=1),
                timedelta(hours=1),
                timedelta(hours=6),
                timedelta(hours=40),
            )
        ]
        self.assertEqual(delays, sorted(delays))
        self.assertGreater(delays[1], timedelta(0))
        self.assertEqual(delays[-1], PSP_POLL_BACKOFF_MAX)


class PollFairnessTests(unittest.IsolatedAsyncioTestCase):
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
                telegram_id=1001,
                username="u",
                full_name="u",
                referral_code="r1",
                role=Role.USER.value,
            )
            session.add(user)
            await session.commit()
            self.user_id = int(user.id)
        self.verified_tokens: list[str] = []

    async def asyncTearDown(self) -> None:
        from app.config import get_settings

        await close_client()
        await self.engine.dispose()
        self._tmp.cleanup()
        clear_settings_cache()
        get_settings.cache_clear()

    async def _seed_awaiting(self, token: str, *, age: timedelta) -> int:
        async with self.Session() as session:
            payment = Payment(
                user_id=self.user_id,
                amount=AMOUNT,
                method=PaymentMethod.PSP.value,
                status=PaymentStatus.PENDING.value,
                is_wallet_topup=True,
            )
            session.add(payment)
            await session.flush()
            created = datetime.now(timezone.utc) - age
            settlement = PaymentSettlement(
                payment_id=int(payment.id),
                tenant_key=0,
                channel=CHANNEL_PSP,
                provider="sunpay",
                status=SettlementStatus.AWAITING.value,
                amount=AMOUNT,
                currency="IRT",
                idempotency_key=idem_key_psp(int(payment.id)),
                external_ref=token,
                checkout_url=f"{PAY_URL}/pay/{token}",
                created_at=created,
                updated_at=created,
            )
            session.add(settlement)
            await session.commit()
            return int(settlement.id)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        token = json.loads(request.content)["token"]
        self.verified_tokens.append(token)
        if token != PAID_TOKEN:
            return httpx.Response(
                200, json={"success": 0, "error": {"code": "NOT_PAID", "message": "not paid"}}
            )
        settlement_id = self.paid_settlement_id
        return httpx.Response(
            200,
            json={
                "success": 1,
                "data": {
                    "amount": AMOUNT,
                    "invoice_id": settlement_id,
                    "reference": "REF-1",
                    "paid_at": "2026-09-27 09:30:00",
                },
            },
        )

    async def _poll(self) -> int:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self._handler))
        with (
            patch.object(sunpay_mod, "_http", return_value=client),
            patch.object(sunpay_mod, "_resolve_host_addresses", return_value=["8.8.8.8"]),
            patch("app.services.payment_settlement._public_base", return_value="https://bot.test"),
            patch("app.services.payment_settlement.approve_payment", AsyncMock(return_value=None)),
            patch("app.services.payment_settlement.notify_after_settlement", AsyncMock()),
        ):
            async with self.Session() as session:
                settled = await poll_psp_settlements(session)
        await client.aclose()
        return settled

    async def _status(self, settlement_id: int) -> str:
        async with self.Session() as session:
            return (
                await session.execute(
                    select(PaymentSettlement.status).where(PaymentSettlement.id == settlement_id)
                )
            ).scalar_one()

    async def test_new_payment_is_verified_despite_many_abandoned_checkouts(self) -> None:
        self.paid_settlement_id = 0
        abandoned = PSP_POLL_BATCH + 20
        for i in range(abandoned):
            await self._seed_awaiting(f"abandoned-{i}", age=timedelta(hours=1))
        # Every abandoned checkout has been looked up at least once.
        await self._poll()
        await self._poll()
        self.assertEqual(len(set(self.verified_tokens)), abandoned)

        self.paid_settlement_id = await self._seed_awaiting(PAID_TOKEN, age=timedelta(0))
        for _ in range(3):
            await self._poll()

        self.assertEqual(await self._status(self.paid_settlement_id), SettlementStatus.SETTLED.value)

    async def test_checked_abandoned_checkout_waits_for_its_backoff(self) -> None:
        self.paid_settlement_id = 0
        await self._seed_awaiting("abandoned-0", age=timedelta(hours=1))
        await self._poll()
        await self._poll()
        self.assertEqual(self.verified_tokens, ["abandoned-0"])

    async def test_fresh_unpaid_checkout_is_checked_every_run(self) -> None:
        self.paid_settlement_id = 0
        await self._seed_awaiting("fresh-0", age=timedelta(minutes=1))
        await self._poll()
        await self._poll()
        self.assertEqual(self.verified_tokens, ["fresh-0", "fresh-0"])


class NextCheckMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_rows(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location(
            "m0067", ROOT / "alembic/versions/0067_settlement_next_check_at.py"
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE payment_settlements (id INTEGER PRIMARY KEY, status TEXT)"))
            conn.execute(text("INSERT INTO payment_settlements VALUES (1, 'awaiting'), (2, 'settled')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                inspector = sa.inspect(conn)
                self.assertIn("next_check_at", {c["name"] for c in inspector.get_columns("payment_settlements")})
                self.assertIn(
                    "ix_payment_settlements_next_check_at",
                    {i["name"] for i in inspector.get_indexes("payment_settlements")},
                )
                self.assertEqual(
                    conn.execute(text("SELECT COUNT(*) FROM payment_settlements WHERE next_check_at IS NULL")).scalar(),
                    2,
                )
                migration.downgrade()
                migration.downgrade()
                self.assertNotIn(
                    "next_check_at", {c["name"] for c in sa.inspect(conn).get_columns("payment_settlements")}
                )
                self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM payment_settlements")).scalar(), 2)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
