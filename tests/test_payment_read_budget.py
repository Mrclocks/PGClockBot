"""Paying, renewing and delivering never run under a web page's panel read deadline,
and a delivery still running in another worker is not reopened."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import sqlalchemy as sa
from sqlalchemy import select

from app.db.models import Order, Payment
from app.services.bot_updates import BotUpdateScheduler
from app.services.delivery_recovery_policy import DELIVERY_IN_PROGRESS
from app.services.orders import approve_payment, pay_with_wallet
from app.services.pg_read_resilience import _BUDGET, outside_web_read_budget, web_read_budget
from tests.test_renewal_preview import RenewalFixture


class BudgetScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_decorated_work_reads_without_the_page_budget_and_restores_it(self) -> None:
        seen = []

        @outside_web_read_budget
        async def work() -> str:
            seen.append(_BUDGET.get())
            return "done"

        with web_read_budget(seconds=4) as budget:
            self.assertEqual(await work(), "done")
            self.assertIs(_BUDGET.get(), budget, "the page keeps its budget afterwards")
        self.assertEqual(seen, [None])

    async def test_telegram_update_task_does_not_inherit_the_webhook_budget(self) -> None:
        seen = []

        class Dispatcher:
            async def feed_update(self, bot, update) -> None:
                seen.append(_BUDGET.get())

        update = SimpleNamespace(update_id=1, callback_query=None, event=SimpleNamespace(chat=SimpleNamespace(id=5), from_user=None))
        with web_read_budget(seconds=4):
            task = await BotUpdateScheduler().submit(Dispatcher(), SimpleNamespace(id=1), update)
        await asyncio.wait_for(task, 5)
        self.assertEqual(seen, [None])


class RenewalUnderPageBudgetTests(RenewalFixture):
    async def test_wallet_renewal_started_by_a_request_reads_the_panel_freely(self) -> None:
        budgets = []
        read = self.pg.get_user_by_id

        async def get_user_by_id(user_id):
            budgets.append(_BUDGET.get())
            return await read(user_id)

        self.pg.get_user_by_id = get_user_by_id
        order = await self.create()
        with web_read_budget(seconds=0.001):
            await asyncio.sleep(0.01)  # the page budget is already spent
            delivered = await pay_with_wallet(self.session, order, self.user)
        self.assertEqual(delivered.status, "delivered")
        self.assertTrue(budgets)
        self.assertTrue(all(b is None for b in budgets))


class StaffResumeRaceTests(RenewalFixture):
    async def approved_order_in(self, status: str, *, updated_ago: timedelta) -> Payment:
        order = await self.create()
        payment = Payment(user_id=1, order_id=order.id, amount=order.amount, status="approved", method="psp")
        self.session.add(payment)
        await self.session.flush()
        await self.session.execute(
            sa.update(Order).where(Order.id == order.id).values(
                status=status, updated_at=datetime.now(timezone.utc) - updated_ago,
            )
        )
        await self.session.commit()
        return payment

    async def test_resume_refuses_a_delivery_running_elsewhere(self) -> None:
        payment = await self.approved_order_in("delivering", updated_ago=timedelta(seconds=5))
        with self.assertRaisesRegex(ValueError, DELIVERY_IN_PROGRESS):
            await approve_payment(self.session, payment, reviewer_tg=0)
        order = await self.session.scalar(select(Order).where(Order.id == payment.order_id))
        self.assertEqual(order.status, "delivering")
        self.assertEqual(self.pg.writes, [])

    async def test_resume_reopens_a_crashed_delivery(self) -> None:
        payment = await self.approved_order_in("delivering", updated_ago=timedelta(minutes=30))
        delivered = await approve_payment(self.session, payment, reviewer_tg=0)
        self.assertEqual(delivered.status, "delivered")
        self.assertEqual(len(self.pg.writes), 1)


if __name__ == "__main__":
    unittest.main()
