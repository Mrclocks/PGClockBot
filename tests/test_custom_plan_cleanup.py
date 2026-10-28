"""Per-order custom plans stay out of catalog lists and are dropped when the order is never sold."""

from __future__ import annotations

import unittest

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, Order, OrderStatus, Payment, PaymentStatus, Plan
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app.services.orders import cancel_order, cancel_stale_pending_orders, reject_payment
from app.services.plans_catalog import list_catalog_plans

OWNER = {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_status": "active"}


class CustomPlanCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=1, telegram_id=101, referral_code="buyer"),
            Plan(id=1, name="Fixed", price=10, duration_days=30, data_limit_gb=1, is_active=True),
        ])
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def _custom_order(self, plan_id: int, status: str) -> Order:
        self.session.add(Plan(id=plan_id, name="پلن دلخواه", price=5, duration_days=9, data_limit_gb=2,
                              is_active=False, sort_order=9999))
        order = Order(user_id=1, plan_id=plan_id, amount=5, status=status, note="custom")
        self.session.add(order)
        await self.session.commit()
        return order

    async def _plan_ids(self) -> list[int]:
        return list((await self.session.scalars(select(Plan.id).order_by(Plan.id))).all())

    async def test_catalog_list_hides_per_order_custom_plans(self) -> None:
        await self._custom_order(2, OrderStatus.PAID.value)
        plans = await list_catalog_plans(self.session, OWNER)
        self.assertEqual([p.id for p in plans], [1])

    async def test_cancelled_unpaid_custom_order_drops_its_plan(self) -> None:
        order = await self._custom_order(2, OrderStatus.PENDING.value)
        await cancel_order(self.session, order)
        self.assertEqual(await self._plan_ids(), [1])
        self.assertEqual(order.status, OrderStatus.CANCELLED.value)

    async def test_rejected_receipt_drops_the_custom_plan(self) -> None:
        order = await self._custom_order(2, OrderStatus.AWAITING_APPROVAL.value)
        payment = Payment(order_id=order.id, user_id=1, amount=5, method="card", status=PaymentStatus.PENDING.value)
        self.session.add(payment)
        await self.session.commit()
        await reject_payment(self.session, payment, reviewer_tg=7)
        self.assertEqual(await self._plan_ids(), [1])
        self.assertEqual(await self.session.scalar(select(func.count(Order.id))), 1)

    async def test_stale_pending_cleanup_drops_only_stale_unpaid_custom_plans(self) -> None:
        stale = await self._custom_order(2, OrderStatus.PENDING.value)
        fresh = await self._custom_order(3, OrderStatus.PENDING.value)
        paid = await self._custom_order(4, OrderStatus.PAID.value)
        old = datetime.now(timezone.utc) - timedelta(hours=100)
        await self.session.execute(update(Order).where(Order.id.in_([stale.id, paid.id])).values(created_at=old))
        await self.session.commit()
        cancelled = await cancel_stale_pending_orders(self.session, older_than_hours=48)
        self.assertEqual(cancelled, 1)
        self.assertEqual(await self._plan_ids(), [1, 3, 4])
        self.assertEqual(await self.session.scalar(select(Order.status).where(Order.id == fresh.id)), "pending")

    async def test_plan_shared_with_another_order_is_kept(self) -> None:
        order = await self._custom_order(2, OrderStatus.PENDING.value)
        self.session.add(Order(user_id=1, plan_id=2, amount=5, status=OrderStatus.PAID.value, note="custom"))
        await self.session.commit()
        await cancel_order(self.session, order)
        self.assertEqual(await self._plan_ids(), [1, 2])


if __name__ == "__main__":
    unittest.main()
