"""A brand-new account waits before taking the free trial, and a refusal does not burn the trial."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, Plan, Role, TrialClaim
from app.services.orders import create_order
from app.services.trial_guard import MAX_WAIT_MINUTES, parse_min_account_age, trial_wait_minutes
from app.services.users import DEFAULT_SETTINGS, set_setting

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


class WaitMathTests(unittest.TestCase):
    def test_the_wait_counts_down_in_whole_minutes_and_ends(self) -> None:
        created = NOW - timedelta(minutes=2, seconds=30)
        self.assertEqual(trial_wait_minutes(created, min_age_minutes=10, now=NOW), 8)
        self.assertEqual(trial_wait_minutes(NOW - timedelta(minutes=10), min_age_minutes=10, now=NOW), 0)
        self.assertEqual(trial_wait_minutes(NOW - timedelta(days=3), min_age_minutes=10, now=NOW), 0)

    def test_a_naive_database_timestamp_is_read_as_utc(self) -> None:
        naive = (NOW - timedelta(minutes=1)).replace(tzinfo=None)
        self.assertEqual(trial_wait_minutes(naive, min_age_minutes=5, now=NOW), 4)

    def test_the_gate_is_off_for_zero_and_for_junk_settings(self) -> None:
        self.assertEqual(trial_wait_minutes(NOW, min_age_minutes=0, now=NOW), 0)
        for raw in (None, "", "abc", "-5", "1.5", "٣", True, "0"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_min_account_age(raw), 0)
        self.assertEqual(parse_min_account_age("15"), 15)
        self.assertEqual(parse_min_account_age("999999999"), MAX_WAIT_MINUTES)

    def test_the_default_leaves_the_gate_off(self) -> None:
        self.assertEqual(DEFAULT_SETTINGS["trial_min_account_minutes"], "0")


class TrialAgeGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'a.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _setup(self, session, *, age_minutes: float, gate: str):
        user = BotUser(
            telegram_id=71, username="u71", full_name="u71", role=Role.USER.value, referral_code="R71",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
        )
        plan = Plan(name="trial", price=0, duration_days=1, data_limit_gb=1, is_active=True, is_trial=True)
        session.add_all([user, plan])
        await session.commit()
        await set_setting(session, "trial_min_account_minutes", gate)
        return user, plan

    async def _order(self, session, user, plan):
        with patch("app.services.users.current_shop_reseller_id", return_value=None):
            return await create_order(session, user_id=user.id, plan_id=plan.id)

    async def _claims(self, session) -> int:
        return int(await session.scalar(select(func.count()).select_from(TrialClaim)) or 0)

    async def test_a_young_account_is_refused_and_keeps_its_trial(self) -> None:
        async with self.Session() as session:
            user, plan = await self._setup(session, age_minutes=1, gate="10")
            with self.assertRaisesRegex(ValueError, "دقیقه"):
                await self._order(session, user, plan)
            self.assertEqual(await self._claims(session), 0)

    async def test_the_same_account_gets_the_trial_once_it_is_old_enough(self) -> None:
        async with self.Session() as session:
            user, plan = await self._setup(session, age_minutes=30, gate="10")
            order = await self._order(session, user, plan)
            self.assertEqual(await self._claims(session), 1)
        self.assertEqual(order.user_id, user.id)

    async def test_with_the_gate_off_a_new_account_gets_the_trial_at_once(self) -> None:
        async with self.Session() as session:
            user, plan = await self._setup(session, age_minutes=0, gate="0")
            await self._order(session, user, plan)
            self.assertEqual(await self._claims(session), 1)


if __name__ == "__main__":
    unittest.main()
