"""A delivered order stays delivered, and the later reward steps still run, when one step fails.

deliver_order commits the delivery, then runs the referral bonus, the loyalty step and cashback on the
same session. Two ways this went wrong:

- Without a rollback, a database error in one step left the transaction aborted. Every later step
  (loyalty, cashback, the funnel record) then failed and was only logged.
- With a plain rollback, the delivered order, reloaded in that transaction, was expired. An async
  session cannot load an expired attribute on access, so the next ``order.id`` raised MissingGreenlet
  and deliver_order failed after the order had been delivered.

A failed step therefore rolls back and reloads the order (db_safe.rollback_and_reload).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select, text

from app.db.models import BotUser, FunnelEvent
from app.services import orders
from app.services.users import set_setting
from tests.test_funnel_events import FunnelCase


class DeliveryAfterRewardFailureTests(FunnelCase):
    async def _assert_delivered(self, delivered) -> None:
        self.assertEqual(delivered.status, "delivered")
        self.assertIsNotNone(delivered.service_id)
        steps = await self.session.scalar(select(func.count()).select_from(FunnelEvent).where(FunnelEvent.order_id == 12))
        self.assertEqual(steps, 1, "the steps after the failed one still ran")

    async def test_a_cashback_failure_after_delivery_leaves_the_order_delivered(self) -> None:
        with patch("app.services.cashback.grant_for_order", AsyncMock(side_effect=RuntimeError("cashback down"))):
            delivered = await self.deliver()
        await self._assert_delivered(delivered)

    async def test_a_database_error_in_the_referral_bonus_leaves_the_order_delivered(self) -> None:
        buyer = await self.session.get(BotUser, 1)
        buyer.referred_by_id = 2
        await self.session.commit()
        await set_setting(self.session, "referral_bonus", "3000", reseller_id=0)

        async def database_error(session, *_args, **_kwargs):
            # Any statement that fails aborts the open transaction, like a failed ledger insert.
            await session.execute(text("SELECT * FROM no_such_table"))

        with patch.object(orders, "credit_wallet", database_error):
            delivered = await self.deliver()
        await self._assert_delivered(delivered)
