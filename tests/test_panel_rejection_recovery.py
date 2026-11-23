"""A PasarGuard 422 is reported with the panel's reason and is not retried again and again."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.db.models import DeliveryFailure, Order, Payment
from app.services import paid_order_recovery as recovery
from app.services.pasarguard import PasarGuardError, failure_text, is_panel_rejection
from tests.test_paid_order_recovery import RecoveryFixture, connect_error, later

TOO_LONG = '{"detail": [{"loc": ["body", "username"], "msg": "String should have at most 32 characters"}]}'


def rejection() -> PasarGuardError:
    return PasarGuardError("POST /api/user/from_template failed (422)", 422, TOO_LONG)


class FailureTextTests(unittest.TestCase):
    def test_the_panels_reason_is_kept_with_the_status_line(self) -> None:
        self.assertEqual(
            failure_text(rejection()),
            "POST /api/user/from_template failed (422): username: String should have at most 32 characters",
        )

    def test_a_reason_behind_a_service_error_is_found_too(self) -> None:
        try:
            try:
                raise rejection()
            except PasarGuardError as exc:
                raise ValueError("delivery failed") from exc
        except ValueError as wrapped:
            self.assertIn("at most 32 characters", failure_text(wrapped))
            self.assertTrue(is_panel_rejection(wrapped))

    def test_other_errors_are_unchanged_and_not_rejections(self) -> None:
        self.assertEqual(failure_text(RuntimeError("boom")), "boom")
        for status in (409, 500, 502):
            self.assertFalse(is_panel_rejection(PasarGuardError("x", status, "")), status)
        self.assertTrue(is_panel_rejection(rejection()))


class RecoveryTests(RecoveryFixture):
    async def paid_purchase(self) -> int:
        order = Order(user_id=1, plan_id=1, amount=300, status="paid", payment_method="psp")
        self.session.add(order)
        await self.session.flush()
        self.session.add(Payment(user_id=1, order_id=order.id, amount=300, status="approved", method="psp"))
        self.session.add(DeliveryFailure(order_id=order.id, error="x", next_auto_retry_at=later(minutes=-1)))
        await self.session.commit()
        return int(order.id)

    async def test_a_rejected_purchase_is_refunded_on_the_first_retry_with_the_reason_kept(self) -> None:
        order_id = await self.paid_purchase()
        self.pg.create_user_from_template = AsyncMock(side_effect=rejection())
        with patch("app.services.orders._hold_and_charge_delivery", AsyncMock(return_value=(None, None))):
            results = await self.run_due(panel_up=True, now=later())
            self.assertEqual(await self.run_due(panel_up=True, now=later(hours=2)), [])
        self.assertEqual([(r.outcome, r.reason) for r in results], [(recovery.Outcome.REFUNDED, recovery.REASON_REJECTED)])
        self.assertEqual(self.pg.create_user_from_template.await_count, 1)
        self.assertEqual(await self.wallet_balance(), 1000 + 300)
        failure = await self.failure_of(order_id)
        self.assertIn("username: String should have at most 32 characters", failure.error)

    async def test_an_unreachable_panel_is_still_retried(self) -> None:
        order_id = await self.paid_purchase()
        self.pg.create_user_from_template = AsyncMock(side_effect=connect_error())
        with patch("app.services.orders._hold_and_charge_delivery", AsyncMock(return_value=(None, None))):
            self.assertEqual(await self.run_due(panel_up=True, now=later()), [])
        failure = await self.failure_of(order_id)
        self.assertIsNone(failure.auto_retry_stopped_at)
        self.assertIsNotNone(failure.next_auto_retry_at)
        self.assertEqual(await self.wallet_balance(), 1000)


if __name__ == "__main__":
    unittest.main()
