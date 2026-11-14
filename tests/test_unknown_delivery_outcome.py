"""A paid delivery that may have reached the panel must stay with staff: no refund, no re-delivery."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import payments as stars_handlers
from app.db.models import (
    Base,
    BotUser,
    DeliveryFailure,
    Order,
    OrderStatus,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Plan,
    Role,
    WalletTransaction,
)
from app.services import orders
from app.services.orders import DeliveryAttempt, DeliveryOutcomeUnknown

PRICE = 40_000
START_BALANCE = 100_000


def _panel_may_hold_failure(session_attempt_holder: dict):
    async def fulfill(session, order, *, attempt=None):
        attempt.panel_may_hold_account = session_attempt_holder["unknown"]
        raise RuntimeError("panel create timed out")

    return fulfill


class _Fixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.sessions()
        user = BotUser(
            telegram_id=9301, referral_code="U9301", role=Role.USER.value, wallet_balance=START_BALANCE
        )
        plan = Plan(name="P", price=PRICE, data_limit_gb=10, duration_days=30, is_active=True)
        self.session.add_all([user, plan])
        await self.session.flush()
        self.order = Order(user_id=user.id, plan_id=plan.id, amount=PRICE, status=OrderStatus.PENDING.value)
        self.session.add(self.order)
        await self.session.commit()
        self.user = user

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def _balance(self) -> int:
        await self.session.refresh(self.user)
        return int(self.user.wallet_balance)

    async def _open_failures(self) -> int:
        rows = await self.session.scalars(
            select(DeliveryFailure).where(DeliveryFailure.resolved_at.is_(None))
        )
        return len(list(rows))


class WalletUnknownOutcomeTests(_Fixture):
    async def test_unknown_outcome_keeps_the_debit_and_lists_the_order_for_staff(self) -> None:
        holder = {"unknown": True}
        with patch.object(orders, "fulfill_paid_order", _panel_may_hold_failure(holder)):
            with self.assertRaises(DeliveryOutcomeUnknown):
                await orders.pay_with_wallet(self.session, self.order, self.user)
        self.assertEqual(await self._balance(), START_BALANCE - PRICE)
        await self.session.refresh(self.order)
        self.assertEqual(self.order.status, OrderStatus.PAID.value)
        self.assertEqual(await self._open_failures(), 1)

    async def test_buyer_tapping_pay_again_does_not_deliver_a_second_time(self) -> None:
        holder = {"unknown": True}
        with patch.object(orders, "fulfill_paid_order", _panel_may_hold_failure(holder)):
            with self.assertRaises(DeliveryOutcomeUnknown):
                await orders.pay_with_wallet(self.session, self.order, self.user)
        retry = AsyncMock()
        with patch.object(orders, "fulfill_paid_order", retry):
            with self.assertRaises(DeliveryOutcomeUnknown):
                await orders.pay_with_wallet(self.session, self.order, self.user)
        retry.assert_not_awaited()
        self.assertEqual(await self._balance(), START_BALANCE - PRICE)

    async def test_a_failure_the_panel_never_saw_still_refunds_and_reopens_the_order(self) -> None:
        holder = {"unknown": False}
        with patch.object(orders, "fulfill_paid_order", _panel_may_hold_failure(holder)):
            with self.assertRaises(RuntimeError):
                await orders.pay_with_wallet(self.session, self.order, self.user)
        self.assertEqual(await self._balance(), START_BALANCE)
        await self.session.refresh(self.order)
        self.assertEqual(self.order.status, OrderStatus.PENDING.value)
        refunds = await self.session.scalars(
            select(WalletTransaction).where(WalletTransaction.amount == PRICE, WalletTransaction.reason.like("برگشت%"))
        )
        self.assertEqual(len(list(refunds)), 1)


class StarsUnknownOutcomeTests(_Fixture):
    async def _stars_payment(self) -> Payment:
        self.order.status = OrderStatus.AWAITING_RECEIPT.value
        payment = Payment(
            order_id=self.order.id, user_id=self.user.id, amount=PRICE,
            method=PaymentMethod.STARS.value, status=PaymentStatus.PENDING.value,
        )
        self.session.add(payment)
        await self.session.commit()
        return payment

    def _message(self, payment: Payment, charge_id: str) -> MagicMock:
        bot = MagicMock()
        bot.refund_star_payment = AsyncMock(return_value=True)
        message = MagicMock()
        message.bot = bot
        message.answer = AsyncMock()
        stars = orders.stars_amount_for_toman(PRICE, 500)
        message.successful_payment = SimpleNamespace(
            currency="XTR", total_amount=stars, telegram_payment_charge_id=charge_id,
            invoice_payload=f"stars:{payment.id}:{stars}",
        )
        return message

    async def _run(self, *, unknown: bool, charge_id: str) -> tuple[MagicMock, Payment]:
        payment = await self._stars_payment()
        message = self._message(payment, charge_id)

        async def approve(session, pay, reviewer_tg, *, attempt=None, **_kw):
            attempt.panel_may_hold_account = unknown
            raise RuntimeError("panel create timed out")

        with patch.object(stars_handlers, "approve_payment", approve), patch.object(
            stars_handlers, "notify_stars_delivery_failed", AsyncMock()
        ):
            await stars_handlers.stars_successful_payment(message, self.session, self.user)
        await self.session.refresh(payment)
        return message, payment

    async def test_unknown_outcome_does_not_refund_the_stars(self) -> None:
        message, payment = await self._run(unknown=True, charge_id="chg_unknown")
        message.bot.refund_star_payment.assert_not_awaited()
        self.assertNotEqual(payment.status, PaymentStatus.REJECTED.value)
        self.assertEqual(await self._open_failures(), 1)
        self.assertIn("بررسی", message.answer.await_args.args[0])

    async def test_a_failure_the_panel_never_saw_still_refunds_the_stars(self) -> None:
        message, payment = await self._run(unknown=False, charge_id="chg_clean")
        message.bot.refund_star_payment.assert_awaited_once()
        self.assertEqual(payment.status, PaymentStatus.REJECTED.value)


class AttemptDefaultsTests(unittest.TestCase):
    def test_a_fresh_attempt_assumes_the_panel_holds_nothing(self) -> None:
        self.assertFalse(DeliveryAttempt().panel_may_hold_account)


if __name__ == "__main__":
    unittest.main()
