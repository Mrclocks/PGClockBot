"""Daily reports: first-delivery time, the report-day funnel, per-shop isolation and the migration."""

from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import sqlalchemy as sa
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import (
    Base,
    BotUser,
    FunnelEvent,
    Order,
    OrderStatus,
    ResellerProfile,
    ResellerSetting,
    Setting,
    delivered_stamp,
)
from app.services.daily_report import previous_tehran_day_bounds
from app.services.daily_report_delivery import deliver_daily_reports
from app.services.users import clear_settings_cache
from app.services.ux20 import funnel_summary
from tests.postgres_support import scoped_async_engine, scoped_sync_engine

ROOT = Path(__file__).resolve().parents[1]
# 02:00 UTC on 10 Nov 2026 is 05:30 in Tehran: past the default 04:00 send hour.
RUN_AT = datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)


class DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        clear_settings_cache()
        self.addCleanup(clear_settings_cache)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def add_user(self, session, user_id: int, *, role: str = "user") -> BotUser:
        user = BotUser(id=user_id, telegram_id=9000 + user_id, referral_code=f"u{user_id}", role=role)
        session.add(user)
        await session.flush()
        return user


class DeliveredAtTests(DbCase):
    async def test_first_delivery_is_stamped_once(self) -> None:
        async with self.Session() as session:
            await self.add_user(session, 1)
            order = Order(user_id=1, amount=100, status=OrderStatus.PAID.value)
            session.add(order)
            await session.commit()
            self.assertIsNone(order.delivered_at)
            order.status = OrderStatus.DELIVERED.value
            first = order.delivered_at
            self.assertIsNotNone(first)
            order.status = OrderStatus.DELIVERED.value
            self.assertEqual(order.delivered_at, first)

    async def test_bulk_update_keeps_the_earliest_delivery_time(self) -> None:
        early = datetime(2026, 1, 1, tzinfo=timezone.utc)
        later = datetime(2026, 2, 1, tzinfo=timezone.utc)
        async with self.Session() as session:
            await self.add_user(session, 1)
            session.add(Order(id=5, user_id=1, amount=100, status=OrderStatus.DELIVERING.value))
            await session.commit()
            for when in (early, later):
                await session.execute(
                    update(Order).where(Order.id == 5).values(
                        status=OrderStatus.DELIVERED.value, **delivered_stamp(OrderStatus.DELIVERED.value, now=when)
                    )
                )
            await session.commit()
            stored = await session.scalar(select(Order.delivered_at).where(Order.id == 5))
            self.assertEqual(stored.replace(tzinfo=timezone.utc), early)

    def test_stamp_is_empty_for_other_statuses(self) -> None:
        self.assertEqual(delivered_stamp(OrderStatus.PAID.value), {})


class FunnelWindowTests(DbCase):
    async def test_report_funnel_counts_only_the_report_day(self) -> None:
        start, end, _ = previous_tehran_day_bounds(RUN_AT)
        async with self.Session() as session:
            await self.add_user(session, 1)
            session.add_all([
                FunnelEvent(user_id=1, step="delivered", idempotency_key="in", created_at=start + timedelta(hours=1)),
                FunnelEvent(user_id=1, step="delivered", idempotency_key="after", created_at=end + timedelta(hours=1)),
            ])
            await session.commit()
            counts = await funnel_summary(session, reseller_id=None, window=(start, end))
        self.assertEqual(counts["delivered"], 1)


class ShopIsolationTests(DbCase):
    async def test_a_failing_shop_does_not_stop_the_next_shop(self) -> None:
        async with self.Session() as session:
            for shop_id in (10, 20):
                await self.add_user(session, shop_id, role="reseller")
                session.add(ResellerProfile(user_id=shop_id, is_active=True, bot_token="sealed"))
                session.add(ResellerSetting(reseller_user_id=shop_id, key="admin_daily_report_enabled", value="1"))
            session.add(Setting(key="admin_daily_report_enabled", value="0"))
            session.add(Setting(key="subordinate_report_enabled", value="0"))
            await session.commit()

        async def build(_session, *, reseller_id, **_kw):
            if reseller_id == 10:
                raise RuntimeError("broken template")
            return "📊", {}

        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=1), message_id=1))
        async with self.Session() as session:
            with patch("app.services.daily_report_delivery.build_daily_report_outbound", side_effect=build), \
                    patch("app.services.daily_report_delivery.open_notify_bot_for_reseller", AsyncMock(return_value=(bot, False))), \
                    patch("app.services.daily_report_delivery.shop_daily_report_chat_ids", AsyncMock(return_value=[77])):
                await deliver_daily_reports(session, bot, now=RUN_AT)
        self.assertEqual(bot.send_message.await_count, 1)
        async with self.Session() as session:
            marks = dict((await session.execute(
                select(ResellerSetting.reseller_user_id, ResellerSetting.value).where(ResellerSetting.key == "admin_daily_report_last")
            )).all())
        self.assertEqual(marks, {20: "2026-11-10"})


class MigrationTests(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location("m0115", ROOT / "alembic/versions/0115_daily_report_accuracy.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_upgrade_twice_backfills_and_shifts_hours_once_then_downgrades(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        migration = self.load()
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE orders (id INTEGER PRIMARY KEY, reseller_id INTEGER, status TEXT, updated_at TIMESTAMPTZ)"))
            conn.execute(sa.text("CREATE TABLE settings (id SERIAL PRIMARY KEY, key TEXT, value TEXT)"))
            conn.execute(sa.text("CREATE TABLE reseller_settings (id SERIAL PRIMARY KEY, reseller_user_id INTEGER, key TEXT, value TEXT)"))
            conn.execute(sa.text("INSERT INTO orders VALUES (1, NULL, 'delivered', '2026-01-02 10:00:00+00'), (2, NULL, 'paid', '2026-01-03 00:00:00+00')"))
            conn.execute(sa.text("INSERT INTO settings (key, value) VALUES ('admin_daily_report_hour', '0')"))
            conn.execute(sa.text("INSERT INTO reseller_settings (reseller_user_id, key, value) VALUES (5, 'admin_daily_report_hour', '22')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                self.assertEqual(
                    conn.execute(sa.text("SELECT id, delivered_at FROM orders ORDER BY id")).all(),
                    [(1, datetime(2026, 1, 2, 10, tzinfo=timezone.utc)), (2, None)],
                )
                hours = lambda: (  # noqa: E731 - small local reader
                    conn.execute(sa.text("SELECT value FROM settings WHERE key = 'admin_daily_report_hour'")).scalar(),
                    conn.execute(sa.text("SELECT value FROM reseller_settings WHERE key = 'admin_daily_report_hour'")).scalar(),
                )
                self.assertEqual(hours(), ("4", "2"))
                migration.downgrade()
                self.assertEqual(hours(), ("0", "22"))
                self.assertNotIn("delivered_at", {c["name"] for c in sa.inspect(conn).get_columns("orders")})
                self.assertEqual(conn.execute(sa.text("SELECT count(*) FROM orders")).scalar(), 2)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
