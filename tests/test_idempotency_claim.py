"""Atomic Setting-key claims for Mini App idempotency."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, Setting
from app.services.idempotency_claim import claim_once, claim_setting_key


class IdempotencyClaimTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # File DB so concurrent sessions share one store (in-memory does not).
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'claim.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self.tmp.cleanup()

    async def test_second_claim_fails(self) -> None:
        key = claim_setting_key("t", 1, "abc")
        async with self.Session() as session:
            self.assertTrue(await claim_once(session, key))
            await session.commit()
        async with self.Session() as session:
            self.assertFalse(await claim_once(session, key))
            await session.commit()
        async with self.Session() as session:
            count = await session.scalar(select(func.count()).select_from(Setting))
            self.assertEqual(int(count), 1)

    async def test_concurrent_claims_only_one_wins(self) -> None:
        key = claim_setting_key("t", 2, "race")

        async def attempt() -> bool:
            async with self.Session() as session:
                won = await claim_once(session, key)
                await session.commit()
                return won

        results = await asyncio.gather(attempt(), attempt(), attempt())
        self.assertEqual(sum(1 for won in results if won), 1)
        async with self.Session() as session:
            count = await session.scalar(select(func.count()).select_from(Setting))
            self.assertEqual(int(count), 1)


if __name__ == "__main__":
    unittest.main()
