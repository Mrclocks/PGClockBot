"""A reward step that fails on the database leaves the session usable for the steps after it.

deliver_order commits the order, then pays the referral bonus, consumes the loyalty discount and grants
cashback, all on the same session. A failed flush (the unique referral key hit by two deliveries at
once) used to leave that session needing a rollback, so the later steps failed and were only logged:
the customer silently lost the cashback.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import func, select, text

from app.db.models import BotUser, Order, OrderStatus, WalletTransaction
from app.services import orders
from app.services.users import set_setting
from tests.test_cross_shop_loyalty_isolation import CUSTOMER, PLATFORM_REFERRER, CrossShopCase


class ReferralRaceTests(CrossShopCase):
    async def _order(self, session) -> Order:
        await set_setting(session, "referral_bonus", "3000", reseller_id=0)
        order = Order(user_id=CUSTOMER, amount=50_000, status=OrderStatus.DELIVERED.value)
        session.add(order)
        await session.commit()
        return order

    async def test_the_session_is_usable_after_a_failed_ledger_write(self) -> None:
        async def failed_ledger_write(session, user, amount, reason, **_kwargs):
            # Two ledger rows in one flush, written the way a second delivery would write them.
            session.add(WalletTransaction(user_id=user.id, amount=amount, balance_after=amount, reason=reason))
            session.add(WalletTransaction(user_id=user.id, amount=amount, balance_after=amount, reason=reason))
            await session.flush()

        async with self.Session() as session:
            order = await self._order(session)
            with patch.object(orders, "credit_wallet", failed_ledger_write):
                await orders._maybe_pay_referral_bonus(session, order)
            self.assertEqual(await session.scalar(text("SELECT 1")), 1, "the next step can still query")

    async def test_an_unexpected_bonus_error_also_leaves_the_session_usable(self) -> None:
        async def broken(session, *_args, **_kwargs):
            await session.execute(text("SELECT * FROM no_such_table"))

        async with self.Session() as session:
            order = await self._order(session)
            with patch.object(orders, "credit_wallet", broken), self.assertLogs(orders.logger, "WARNING"):
                await orders._maybe_pay_referral_bonus(session, order)
            self.assertEqual(await session.scalar(text("SELECT 1")), 1)

    async def test_the_bonus_is_still_paid_once_when_nothing_fails(self) -> None:
        async with self.Session() as session:
            order = await self._order(session)
            await orders._maybe_pay_referral_bonus(session, order)
            await session.commit()
            await orders._maybe_pay_referral_bonus(session, order)
            await session.commit()
            count = await session.scalar(select(func.count()).select_from(WalletTransaction).where(
                WalletTransaction.user_id == PLATFORM_REFERRER, WalletTransaction.reason == f"referral:{CUSTOMER}"))
            self.assertEqual(count, 1)
            self.assertIsNotNone(await session.get(BotUser, PLATFORM_REFERRER))


if __name__ == "__main__":
    unittest.main()
