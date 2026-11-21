"""The finance delivery tab diagnoses all its orders with the same queries, however many there are.

It used to look up the open delivery failure of each order (by order, then by payment) one at a
time: two queries per row, up to two hundred for a full page. The batch must give every row the same
diagnosis the one-at-a-time lookup gives.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import BotUser, DeliveryFailure, Order, Payment, Plan, UserService
from app.services.payment_review_diag import (
    diagnose_order_deliveries,
    diagnose_order_delivery,
    load_open_failure_keys,
)
from tests.test_update_owner_only import OWNER_SESSION, RoutesHarness

NOW = datetime(2026, 1, 10, 9, 30, tzinfo=timezone.utc)
FIRST_ORDER = 100
OLDER_ORDER_OFFSET = 10_000


def _order_rows(first: int, count: int) -> list[object]:
    """Stuck and failed orders in every state the diagnosis tells apart."""
    rows: list[object] = []
    for n in range(count):
        order_id, kind = first + n, n % 5
        service_id = order_id if kind in (1, 2) else None
        if service_id:
            rows.append(UserService(id=service_id, bot_user_id=1, plan_id=1, pg_username=f"u{order_id}"))
        rows.append(Order(id=order_id, user_id=1, plan_id=1, amount=1000,
                          status="delivered" if service_id else "paid", service_id=service_id))
        if kind != 4:  # kind 4: an order without any payment
            rows.append(Payment(id=order_id, order_id=order_id, user_id=1, amount=1000, method="card",
                                status="approved", reviewed_by=1))
        if kind == 1:  # open failure recorded on the order
            rows.append(DeliveryFailure(order_id=order_id, error="send", last_attempt_at=NOW))
        if kind == 2:  # open failure known only by its payment: recorded against an older order
            older = order_id + OLDER_ORDER_OFFSET
            rows.append(Order(id=older, user_id=1, plan_id=1, amount=1000, status="cancelled"))
            rows.append(DeliveryFailure(order_id=older, payment_id=order_id, error="send", last_attempt_at=NOW))
        if kind == 3:  # a failure that was resolved does not count
            rows.append(DeliveryFailure(order_id=order_id, error="old", last_attempt_at=NOW, resolved_at=NOW))
    return rows


class _Harness(RoutesHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([BotUser(id=1, telegram_id=101, referral_code="u1", role="user"),
                             Plan(id=1, name="P", price=1000, data_limit_gb=5, duration_days=30)])
            await session.flush()
            for row in _order_rows(FIRST_ORDER, 10):
                session.add(row)
                await session.flush()
            await session.commit()
        self.statements = 0

        def count(*_args: object) -> None:
            self.statements += 1

        event.listen(self.engine.sync_engine, "before_cursor_execute", count)
        self.addCleanup(event.remove, self.engine.sync_engine, "before_cursor_execute", count)

    async def add_orders(self, first: int, count: int) -> None:
        async with self.Session() as session:
            for row in _order_rows(first, count):
                session.add(row)
                await session.flush()
            await session.commit()

    async def orders_and_payments(self) -> tuple[list[Order], dict[int, Payment]]:
        from sqlalchemy import select

        async with self.Session() as session:
            orders = list((await session.scalars(select(Order).order_by(Order.id))).all())
            payments = {int(p.order_id): p for p in (await session.scalars(select(Payment))).all()}
        return orders, payments


class DiagnosisBatchTests(_Harness):
    async def test_the_batch_gives_every_order_the_same_diagnosis_as_one_at_a_time(self) -> None:
        orders, payments = await self.orders_and_payments()
        async with self.Session() as session:
            batch = await diagnose_order_deliveries(session, orders, payments)
            for order in orders:
                single = await diagnose_order_delivery(session, order, payment=payments.get(int(order.id)))
                with self.subTest(order=order.id):
                    self.assertEqual(batch[int(order.id)].as_log_extra(), single.as_log_extra())
        flags = {int(o.id): batch[int(o.id)].has_open_delivery_failure for o in orders}
        self.assertTrue(all(flags[i] for i in (101, 106)), "a failure recorded on the order")
        self.assertTrue(all(flags[i] for i in (102, 107)), "a failure known only by the payment")
        self.assertFalse(any(flags[i] for i in (100, 103, 104, 108, 109)), "no failure, or a resolved one")

    async def test_open_failures_are_read_once_for_any_number_of_orders(self) -> None:
        orders, payments = await self.orders_and_payments()
        async with self.Session() as session:
            self.statements = 0
            await diagnose_order_deliveries(session, orders[:2], payments)
            few = self.statements
            self.statements = 0
            await diagnose_order_deliveries(session, orders, payments)
        self.assertEqual(self.statements, few)
        self.assertEqual(few, 1)

    async def test_the_keys_hold_only_the_orders_and_payments_asked_for(self) -> None:
        async with self.Session() as session:
            keys = await load_open_failure_keys(session, order_ids=[101], payment_ids=[])
            nothing = await load_open_failure_keys(session, order_ids=[], payment_ids=[])
        self.assertEqual((keys.order_ids, keys.payment_ids), (frozenset({101}), frozenset()))
        self.assertEqual((nothing.order_ids, nothing.payment_ids), (frozenset(), frozenset()))


class DeliveryTabRouteTests(_Harness):
    async def statements_for_the_tab(self) -> int:
        cookie = f"session={self.signer.dumps(OWNER_SESSION)}"
        self.statements = 0
        response = await self.client.get("/finance?tab=delivery", headers={"cookie": cookie})
        self.assertEqual(response.status_code, 200)
        return self.statements

    async def test_the_tab_runs_the_same_queries_for_ten_orders_and_for_sixty(self) -> None:
        await self.statements_for_the_tab()  # warms the per-process settings and badge caches
        ten = await self.statements_for_the_tab()
        await self.add_orders(FIRST_ORDER + 10, 50)
        sixty = await self.statements_for_the_tab()
        self.assertEqual(sixty, ten)
