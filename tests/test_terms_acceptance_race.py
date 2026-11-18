"""Accepting the entry terms stays safe when two taps race.

Two «می‌پذیرم» taps used to race on the unique (person, shop, gate) row; the
loser rolled back the whole session, which expired the caller's BotUser, and
the next read of user.role failed with MissingGreenlet (a 500 in the Mini App).
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Base, BotUser, Role, TermsAcceptance
from app.services import terms
from app.services.terms import current_hash, record_acceptance
from tests.postgres_support import scoped_async_engine

UI = {
    "terms_entry_enabled": "1",
    "terms_entry_text": "قوانین ورود",
    "terms_entry_btn": "می‌پذیرم",
}


class TermsAcceptanceRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        async with self.Session() as session:
            user = BotUser(telegram_id=7101, referral_code="race1", role=Role.USER.value)
            session.add(user)
            await session.commit()
            self.user_id = int(user.id)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _rows(self) -> list[TermsAcceptance]:
        async with self.Session() as session:
            return list(await session.scalars(select(TermsAcceptance)))

    async def _accept(self, session: AsyncSession) -> None:
        await record_acceptance(session, bot_user_id=self.user_id, shop_owner_id=0, gate="entry", ui=UI)

    async def test_losing_a_concurrent_first_acceptance_keeps_one_row_and_the_user_readable(self) -> None:
        real_refresh = terms._refresh_acceptance
        calls = 0

        async def other_tap_wins_first(session: AsyncSession, key: tuple, **values: object) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                async with self.Session() as other:
                    other.add(TermsAcceptance(bot_user_id=key[0], shop_owner_id=key[1], gate=key[2], content_hash="old"))
                    await other.commit()
                return False
            return await real_refresh(session, key, **values)

        async with self.Session() as session:
            user = await session.get(BotUser, self.user_id)
            with patch.object(terms, "_refresh_acceptance", other_tap_wins_first):
                await self._accept(session)
            self.assertEqual(user.role, Role.USER.value)
        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].content_hash, current_hash(UI, "entry"))

    async def test_repeated_acceptance_refreshes_the_same_row(self) -> None:
        async with self.Session() as session:
            await self._accept(session)
        changed = {**UI, "terms_entry_text": "قوانین تازه"}
        async with self.Session() as session:
            await record_acceptance(session, bot_user_id=self.user_id, shop_owner_id=0, gate="entry", ui=changed)
        async with self.Session() as session:
            count = await session.scalar(select(func.count()).select_from(TermsAcceptance))
        rows = await self._rows()
        self.assertEqual(count, 1)
        self.assertEqual(rows[0].content_hash, current_hash(changed, "entry"))

    async def test_an_unknown_gate_is_refused(self) -> None:
        async with self.Session() as session:
            with self.assertRaises(ValueError):
                await record_acceptance(session, bot_user_id=self.user_id, shop_owner_id=0, gate="nope", ui=UI)


if __name__ == "__main__":
    unittest.main()
