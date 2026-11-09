"""The notice queue claims before sending, throttles by batch, and never resends an unknown outcome."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import CustomerNotice
from app.services import customer_notice_queue as queue

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


class QueueCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'q.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.sent: list[int] = []

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def add(self, count: int = 1) -> None:
        async with self.Session() as session:
            for i in range(count):
                queue.add_notice(session, shop_id=None, bot_user_id=i + 1, telegram_id=1000 + i, action="service_renew",
                                 service_name="s", reason="تمدید دستی")
            await session.commit()

    async def rows(self) -> list[CustomerNotice]:
        async with self.Session() as session:
            return list((await session.scalars(select(CustomerNotice).order_by(CustomerNotice.id))).all())

    async def run_batch(self, outcome: queue.NoticeOutcome, *, at: datetime = T0, limit: int = 50) -> int:
        async def send(notice: CustomerNotice) -> queue.NoticeOutcome:
            self.sent.append(int(notice.id))
            return outcome

        async with self.Session() as session:
            return await queue.process_notice_batch(session, send, now=at, limit=limit)


class ServiceLinkTests(QueueCase):
    async def test_a_notice_remembers_the_service_it_is_about(self) -> None:
        async with self.Session() as session:
            queue.add_notice(session, shop_id=None, bot_user_id=1, telegram_id=1, action="service_renew",
                             service_name="s", reason="تمدید دستی", service_id=42)
            queue.add_notice(session, shop_id=None, bot_user_id=1, telegram_id=1, action="service_renew",
                             service_name="s", reason="تمدید دستی")
            await session.commit()
        self.assertEqual([r.service_id for r in await self.rows()], [42, None])


class ProcessBatchTests(QueueCase):
    async def test_a_sent_notice_is_marked_sent_and_not_sent_again(self) -> None:
        await self.add()
        await self.run_batch(queue.NoticeOutcome(queue.OUTCOME_SENT))
        await self.run_batch(queue.NoticeOutcome(queue.OUTCOME_SENT), at=T0 + timedelta(minutes=10))
        self.assertEqual(self.sent, [1])
        self.assertEqual([r.status for r in await self.rows()], [queue.STATUS_SENT])

    async def test_a_batch_is_bounded_and_takes_the_oldest_first(self) -> None:
        await self.add(5)
        self.assertEqual(await self.run_batch(queue.NoticeOutcome(queue.OUTCOME_SENT), limit=2), 2)
        self.assertEqual(self.sent, [1, 2])

    async def test_a_retry_waits_for_its_backoff_then_gives_up_after_the_attempt_limit(self) -> None:
        await self.add()
        retry = queue.NoticeOutcome(queue.OUTCOME_RETRY)
        await self.run_batch(retry, at=T0)
        self.assertEqual(await self.run_batch(retry, at=T0 + timedelta(seconds=10)), 0, "not due yet")
        later = T0
        for _ in range(queue.MAX_ATTEMPTS - 1):
            later += timedelta(minutes=5)
            await self.run_batch(retry, at=later)
        row = (await self.rows())[0]
        self.assertEqual((row.status, row.attempts), (queue.STATUS_FAILED, queue.MAX_ATTEMPTS))

    async def test_a_telegram_retry_after_delay_is_honoured(self) -> None:
        await self.add()
        await self.run_batch(queue.NoticeOutcome(queue.OUTCOME_RETRY, timedelta(minutes=30)), at=T0)
        self.assertEqual(await self.run_batch(queue.NoticeOutcome(queue.OUTCOME_SENT), at=T0 + timedelta(minutes=10)), 0)
        self.assertEqual(await self.run_batch(queue.NoticeOutcome(queue.OUTCOME_SENT), at=T0 + timedelta(minutes=31)), 1)

    async def test_a_sender_that_crashes_leaves_an_unknown_outcome_that_is_never_resent(self) -> None:
        await self.add()

        async def crash(_notice: CustomerNotice) -> queue.NoticeOutcome:
            raise RuntimeError("process died after the call")

        async with self.Session() as session:
            await queue.process_notice_batch(session, crash, now=T0)
        self.assertEqual((await self.rows())[0].status, queue.STATUS_SENDING)
        await self.run_batch(queue.NoticeOutcome(queue.OUTCOME_SENT), at=T0 + timedelta(minutes=10))
        self.assertEqual(self.sent, [])
        self.assertEqual((await self.rows())[0].status, queue.STATUS_REVIEW)

    async def test_two_workers_cannot_send_the_same_notice(self) -> None:
        await self.add(3)

        async def worker() -> int:
            return await self.run_batch(queue.NoticeOutcome(queue.OUTCOME_SENT))

        await asyncio.gather(worker(), worker())
        self.assertEqual(sorted(self.sent), [1, 2, 3])


class QueueMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade(self) -> None:
        spec = importlib.util.spec_from_file_location("m0097", ROOT / "alembic/versions/0097_customer_notice_queue.py")
        migration = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        with engine.begin() as conn:
            for down in (False, False, True, True):
                with Operations.context(MigrationContext.configure(conn)):
                    (migration.downgrade if down else migration.upgrade)()
                self.assertEqual(sa.inspect(conn).has_table("customer_notices"), not down)


if __name__ == "__main__":
    unittest.main()
