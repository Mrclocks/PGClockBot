"""Cashback preview at checkout, the buyer notice when cashback is credited, ladder day settings."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, CashbackGrant
from app.services.cashback import credited_text, mature_grants
from app.services.checkout_summary import _cashback_line
from app.services.credit_statements import ladder_days
from app.services.users import set_setting

NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_line_shows_the_amount_the_rules_would_grant(self) -> None:
        order = SimpleNamespace(id=1)
        with patch("app.services.checkout_summary.quote_cashback", AsyncMock(return_value=(3, 12_000))):
            line = await _cashback_line(AsyncMock(), order, "تومان")
        self.assertIn("کش‌بک این خرید", line)
        self.assertIn("12", line)
        with patch("app.services.checkout_summary.quote_cashback", AsyncMock(return_value=(None, 0))):
            self.assertIsNone(await _cashback_line(AsyncMock(), order, "تومان"))

    async def test_a_failed_preview_never_breaks_the_checkout(self) -> None:
        failing = AsyncMock(side_effect=OperationalError("x", {}, Exception()))
        with patch("app.services.checkout_summary.quote_cashback", failing):
            self.assertIsNone(await _cashback_line(AsyncMock(), SimpleNamespace(id=1), "تومان"))


class NoticeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=5, telegram_id=5, referral_code="a"), BotUser(id=6, telegram_id=6, referral_code="b"),
                BotUser(id=9, telegram_id=99, referral_code="c"),
            ])
            for n, shop in ((1, 5), (2, 5), (3, 6)):
                session.add(CashbackGrant(shop_id=shop, user_id=9, order_id=n, base_amount=100, amount=10 * n,
                                          status="pending", available_at=NOW - timedelta(hours=1)))
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_buyers_are_told_once_per_credit_grouped_by_shop(self) -> None:
        batches = AsyncMock()
        with patch("app.services.cashback._send_shop_batch", batches):
            async with self.Session() as session:
                self.assertEqual(await mature_grants(session, now=NOW), 3)
                self.assertEqual(await mature_grants(session, now=NOW), 0)
        sent = {call.args[1]: sorted(int(g.order_id) for g in call.args[2]) for call in batches.await_args_list}
        self.assertEqual(sent, {5: [1, 2], 6: [3]})

    async def test_a_failed_notice_keeps_the_credit(self) -> None:
        with patch("app.services.cashback._send_shop_batch", AsyncMock(side_effect=RuntimeError("down"))):
            async with self.Session() as session:
                self.assertEqual(await mature_grants(session, now=NOW), 3)

    def test_notice_text(self) -> None:
        text = credited_text(SimpleNamespace(amount=10_000, order_id=42))
        self.assertIn("کش‌بک", text)
        self.assertIn("#42", text)


class LadderSettingTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_settings_drive_the_ladder_and_bad_values_fall_back(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.addAsyncCleanup(engine.dispose)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            defaults = await ladder_days(session)
            self.assertEqual((defaults.due, defaults.block_create, defaults.block_changes, defaults.block), (7, 3, 7, 14))
            await set_setting(session, "credit_due_days", "10")
            await set_setting(session, "credit_block_days", "-4")
            await session.commit()
            days = await ladder_days(session)
        self.assertEqual((days.due, days.block), (10, 14))


if __name__ == "__main__":
    unittest.main()
