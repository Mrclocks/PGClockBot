"""Phase 5: owner budget fails closed on growth; untimed move clears expires_at."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, PgAdminSubscription, ResellerPlan
from app.services.billing import GB
from app.services.pg_admin_subscription import (
    STATUS_ACTIVE,
    _assert_owner_capacity_budget,
    _extend_period,
    record_unit_extra,
    renew_subscription,
)


class Phase5FailClosedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_unreadable_owner_blocks_capacity_growth(self) -> None:
        async with self.Session() as session:
            session.add(
                PgAdminSubscription(
                    pg_username="shop3", plan_id=1, access_status=STATUS_ACTIVE,
                    base_gb=10, base_users=5,
                )
            )
            await session.commit()
            pg = SimpleNamespace(get_admin=AsyncMock(side_effect=TimeoutError("down")))
            with (
                patch("app.config.get_settings", return_value=SimpleNamespace(pg_username="owner")),
                patch("app.services.pasarguard.get_pg", return_value=pg),
            ):
                with self.assertRaises(ValueError) as caught:
                    await _assert_owner_capacity_budget(
                        session, target_pg_username="shop3", next_total_gb=20, next_total_users=5,
                        previous_total_gb=10, previous_total_users=5,
                    )
            self.assertIn("خوانده نشد", str(caught.exception))

    async def test_empty_owner_admin_blocks_new_capacity(self) -> None:
        async with self.Session() as session:
            pg = SimpleNamespace(get_admin=AsyncMock(return_value={}))
            with (
                patch("app.config.get_settings", return_value=SimpleNamespace(pg_username="owner")),
                patch("app.services.pasarguard.get_pg", return_value=pg),
            ):
                with self.assertRaises(ValueError):
                    await _assert_owner_capacity_budget(
                        session, target_pg_username="shop3", next_total_gb=1, next_total_users=1,
                        previous_total_gb=0, previous_total_users=0,
                    )

    async def test_unreadable_owner_allows_same_capacity_renew(self) -> None:
        async with self.Session() as session:
            session.add(
                PgAdminSubscription(
                    pg_username="shop3", plan_id=1, access_status=STATUS_ACTIVE,
                    base_gb=10, base_users=5,
                )
            )
            await session.commit()
            pg = SimpleNamespace(get_admin=AsyncMock(side_effect=TimeoutError("down")))
            with (
                patch("app.config.get_settings", return_value=SimpleNamespace(pg_username="owner")),
                patch("app.services.pasarguard.get_pg", return_value=pg),
            ):
                await _assert_owner_capacity_budget(
                    session, target_pg_username="shop3", next_total_gb=10, next_total_users=5,
                    previous_total_gb=10, previous_total_users=5,
                )

    async def test_record_unit_extra_blocks_growth_when_owner_unreadable(self) -> None:
        """Regression: budget must use pre-flush previous totals, not the already-written row."""
        async with self.Session() as session:
            session.add(
                PgAdminSubscription(
                    pg_username="shop3", plan_id=1, access_status=STATUS_ACTIVE,
                    base_gb=10, base_users=5, extra_gb_purchased=0,
                )
            )
            await session.commit()
            pg = SimpleNamespace(get_admin=AsyncMock(side_effect=TimeoutError("down")))
            with (
                patch("app.config.get_settings", return_value=SimpleNamespace(pg_username="owner")),
                patch("app.services.pasarguard.get_pg", return_value=pg),
                patch("app.services.pg_admin_subscription.assert_parent_has_room", AsyncMock()),
            ):
                with self.assertRaises(ValueError) as caught:
                    await record_unit_extra(session, pg_username="shop3", gb=50)
            self.assertIn("خوانده نشد", str(caught.exception))

    def test_extend_period_clears_clock_for_untimed_plan(self) -> None:
        sub = PgAdminSubscription(
            pg_username="shop3", plan_id=1, access_status=STATUS_ACTIVE,
            expires_at=datetime.now(timezone.utc) + timedelta(days=3),
        )
        plan = SimpleNamespace(id=2, duration_days=0, included_gb=50, included_users=10)
        _extend_period(sub, plan, datetime.now(timezone.utc))
        self.assertIsNone(sub.expires_at)
        self.assertEqual(sub.plan_id, 2)
        self.assertEqual(sub.access_status, STATUS_ACTIVE)

    async def test_move_to_untimed_plan_clears_expires_at(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(days=10)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=3, telegram_id=33, referral_code="s3", role="reseller", wallet_balance=1_000_000),
                ResellerPlan(
                    id=1, name="timed", plan_kind="subscription", billing_mode="fixed",
                    duration_days=30, included_users=10, included_gb=100, price=100_000, is_active=True,
                ),
                ResellerPlan(
                    id=2, name="forever", plan_kind="subscription", billing_mode="fixed",
                    duration_days=0, included_users=10, included_gb=100, price=200_000, is_active=True,
                ),
                PgAdminSubscription(
                    pg_username="shop3", plan_id=1, access_status=STATUS_ACTIVE,
                    base_users=10, base_gb=100, renew_generation=0, expires_at=future,
                ),
            ])
            await session.commit()
            from sqlalchemy import select

            sub = await session.scalar(select(PgAdminSubscription))
            plan = await session.get(ResellerPlan, 2)
            owner = await session.get(BotUser, 3)
            pg = SimpleNamespace(
                get_admin=AsyncMock(return_value={"data_limit": 100 * GB, "max_users": 10}),
                modify_admin=AsyncMock(return_value={}),
                reset_admin_usage=AsyncMock(return_value={}),
            )
            with (
                patch("app.services.pasarguard.get_pg", return_value=pg),
                patch("app.services.billing_suspend._set_admin_enabled", AsyncMock()),
                patch("app.services.pg_admin_subscription.assert_child_growth_fits", AsyncMock()),
                patch("app.services.pg_admin_subscription._assert_owner_capacity_budget", AsyncMock()),
            ):
                result = await renew_subscription(
                    session, sub=sub, plan=plan, payer=owner, charge_wallet=True, moving=True,
                )
            self.assertFalse(result.get("timed"))
            await session.refresh(sub)
            self.assertIsNone(sub.expires_at)


if __name__ == "__main__":
    unittest.main()
