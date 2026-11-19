"""Daily Telegram digest covers the previous full Asia/Tehran calendar day."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Base, BotUser, Order, OrderStatus, Plan, UserService
from app.services.daily_report import (
    ACTOR_OWNER,
    previous_day_activity,
    collect_report_values,
    format_jalali_parts,
    previous_tehran_day_bounds,
)
from tests.postgres_support import scoped_async_engine

_TEHRAN = ZoneInfo("Asia/Tehran")


class PreviousDayBoundsTests(unittest.TestCase):
    def test_window_is_previous_tehran_calendar_day(self) -> None:
        # 2026-11-10 00:30 UTC == 04:00 Tehran on the 10th → previous day is the 9th.
        now = datetime(2026, 11, 10, 0, 30, tzinfo=timezone.utc)
        start, end, local = previous_tehran_day_bounds(now)
        self.assertEqual(local.astimezone(_TEHRAN).date().isoformat(), "2026-11-09")
        self.assertEqual(start.astimezone(_TEHRAN).isoformat(), "2026-11-09T00:00:00+03:30")
        self.assertEqual(end.astimezone(_TEHRAN).isoformat(), "2026-11-10T00:00:00+03:30")
        self.assertEqual(end - start, timedelta(days=1))


class PreviousDayActivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.now = datetime(2026, 11, 10, 0, 30, tzinfo=timezone.utc)
        self.start, self.end, self.local = previous_tehran_day_bounds(self.now)
        self.mid = self.start + timedelta(hours=12)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_activity_counts_previous_day_not_partial_today(self) -> None:
        async with self.Session() as session:
            user = BotUser(
                telegram_id=501,
                referral_code="PDUSER1",
                role="user",
                is_demo=False,
                created_at=self.mid,
            )
            plan = Plan(name="P", price=1000, duration_days=30, data_limit_gb=5, is_active=True)
            session.add_all([user, plan])
            await session.commit()
            await session.refresh(user)
            await session.refresh(plan)

            yesterday = Order(
                user_id=user.id,
                plan_id=plan.id,
                amount=7000,
                status=OrderStatus.DELIVERED.value,
                created_at=self.mid,
                updated_at=self.mid,
                delivered_at=self.mid,
            )
            today_partial = Order(
                user_id=user.id,
                plan_id=plan.id,
                amount=9000,
                status=OrderStatus.DELIVERED.value,
                created_at=self.now,
                updated_at=self.now,
                delivered_at=self.now,
            )
            # Registered before the report day, delivered during the report day.
            late_delivery = Order(
                user_id=user.id,
                plan_id=plan.id,
                amount=3000,
                status=OrderStatus.DELIVERED.value,
                created_at=self.start - timedelta(hours=5),
                updated_at=self.mid,
                delivered_at=self.mid,
            )
            # Delivered days earlier and only edited during the report day: not a delivery of that day.
            edited_old = Order(
                user_id=user.id,
                plan_id=plan.id,
                amount=50000,
                status=OrderStatus.DELIVERED.value,
                created_at=self.start - timedelta(days=3),
                delivered_at=self.start - timedelta(days=3),
                updated_at=self.mid,
            )
            session.add_all([yesterday, today_partial, late_delivery, edited_old])
            session.add(
                UserService(
                    bot_user_id=user.id,
                    plan_id=plan.id,
                    pg_username="pd-svc",
                    created_at=self.mid,
                )
            )
            await session.commit()

            with patch(
                "app.services.daily_report.previous_tehran_day_bounds",
                return_value=(self.start, self.end, self.local),
            ):
                activity = await previous_day_activity(session, reseller_id=None)

            self.assertEqual(activity["orders_new"], 1)  # yesterday only
            self.assertEqual(activity["orders_delivered"], 2)  # yesterday + late delivery
            self.assertEqual(activity["revenue_today"], 10000)
            self.assertEqual(activity["services_new"], 1)
            self.assertEqual(activity["users_new"], 1)

    async def test_header_uses_previous_tehran_day(self) -> None:
        async with self.Session() as session:
            with patch(
                "app.services.daily_report.previous_tehran_day_bounds",
                return_value=(self.start, self.end, self.local),
            ):
                values = await collect_report_values(
                    session,
                    reseller_id=None,
                    actor=ACTOR_OWNER,
                    admin_name="مالک",
                    enabled_keys=["date_from", "date_to", "day", "month_name", "year"],
                )
            expected = format_jalali_parts(self.local)
            self.assertEqual(values["date_from"], expected["date"])
            self.assertEqual(values["date_to"], expected["date"])
            self.assertEqual(values["day"], expected["day"])


if __name__ == "__main__":
    unittest.main()
