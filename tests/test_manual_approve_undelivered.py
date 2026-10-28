"""A receipt approved by hand whose service could not be created: the reviewer is told why,
the order is queued under failed deliveries, staff are alerted and the buyer is not left silent."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select

from app.db.models import DeliveryFailure, Order, Payment, UserService
from app.services.orders import create_custom_order
from app.services.payment_review_actions import (
    DELIVERY_FAILED_ALERT,
    DELIVERY_FAILED_HINT,
    TELEGRAM_ALERT_LIMIT,
    execute_payment_approve,
    execute_payment_resume,
)
from tests.test_paid_order_undelivered import SHOP, PanelDown, UndeliveredPaidOrderTestCase


class ManualApproveCases:
    """Run for the platform and for a shop."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.buyer_notice = AsyncMock()
        patcher = patch("app.services.payment_review_actions.send_delivery_to_user", self.buyer_notice)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))

    async def receipt_payment(self) -> SimpleNamespace:
        order = await create_custom_order(self.session, user_id=1, data_limit_gb=10, duration_days=30)
        payment = Payment(
            user_id=1, order_id=order.id, amount=order.amount, status="pending", method="card", receipt_file_id="f"
        )
        self.session.add(payment)
        await self.session.commit()
        return SimpleNamespace(order_id=int(order.id), payment_id=int(payment.id))

    async def approve(self, receipt: SimpleNamespace):
        payment = await self.fresh(Payment, receipt.payment_id)
        return await execute_payment_approve(self.session, payment, reviewer_tg=5, bot=self.bot)

    async def failures(self, receipt: SimpleNamespace) -> list[DeliveryFailure]:
        self.session.expire_all()
        rows = await self.session.scalars(
            select(DeliveryFailure).where(DeliveryFailure.order_id == receipt.order_id)
        )
        return list(rows)

    async def test_reviewer_is_told_the_service_failed_not_that_delivery_continues(self) -> None:
        receipt = await self.receipt_payment()
        result = await self.approve(receipt)
        self.assertFalse(result.ok)
        self.assertTrue(result.alert_fa.startswith(DELIVERY_FAILED_ALERT))
        self.assertIn(DELIVERY_FAILED_HINT, result.alert_fa)
        self.assertNotIn("ادامه می‌یابد", result.alert_fa)
        self.assertLessEqual(len(result.alert_fa), TELEGRAM_ALERT_LIMIT)
        self.assertEqual((await self.fresh(Order, receipt.order_id)).status, "paid")
        self.assertEqual((await self.fresh(Payment, receipt.payment_id)).status, "approved")

    async def test_failure_is_queued_with_its_reason_and_staff_are_alerted(self) -> None:
        receipt = await self.receipt_payment()
        await self.approve(receipt)
        failures = await self.failures(receipt)
        self.assertEqual(len(failures), 1)
        self.assertIn("panel timeout", failures[0].error)
        self.assertEqual(failures[0].payment_id, receipt.payment_id)
        self.stuck_notice.assert_awaited_once()
        self.assertIn("panel timeout", self.stuck_notice.await_args.kwargs["error"])

    async def test_buyer_is_told_the_service_is_being_prepared(self) -> None:
        receipt = await self.receipt_payment()
        await self.approve(receipt)
        self.buyer_notice.assert_awaited_once()
        self.assertEqual(self.buyer_notice.await_args.args[1], 11)

    async def test_secrets_in_the_panel_error_are_not_stored(self) -> None:
        self.pg.create_user = AsyncMock(side_effect=PanelDown("login failed token=abc123secret"))
        receipt = await self.receipt_payment()
        result = await self.approve(receipt)
        failures = await self.failures(receipt)
        self.assertNotIn("abc123secret", failures[0].error)
        self.assertNotIn("abc123secret", self.stuck_notice.await_args.kwargs["error"])
        self.assertNotIn("abc123secret", result.alert_fa)

    async def test_a_long_panel_error_still_fits_the_alert(self) -> None:
        self.pg.create_user = AsyncMock(side_effect=PanelDown("x" * 900))
        receipt = await self.receipt_payment()
        result = await self.approve(receipt)
        self.assertLessEqual(len(result.alert_fa), TELEGRAM_ALERT_LIMIT)
        self.assertIn(DELIVERY_FAILED_HINT, result.alert_fa)

    async def test_failing_notices_do_not_hide_the_failure_from_the_reviewer(self) -> None:
        self.stuck_notice.side_effect = RuntimeError("telegram down")
        self.buyer_notice.side_effect = RuntimeError("telegram down")
        receipt = await self.receipt_payment()
        result = await self.approve(receipt)
        self.assertTrue(result.alert_fa.startswith(DELIVERY_FAILED_ALERT))
        self.assertEqual(len(await self.failures(receipt)), 1)

    async def test_resume_after_the_failure_creates_one_service_and_no_second_alert(self) -> None:
        receipt = await self.receipt_payment()
        await self.approve(receipt)
        self.pg.create_user = AsyncMock(return_value={"id": 9, "username": "u9", "subscription_url": "https://x/sub/t"})
        payment = await self.fresh(Payment, receipt.payment_id)
        resumed = await execute_payment_resume(self.session, payment, reviewer_tg=5, bot=self.bot)
        self.assertTrue(resumed.ok)
        self.session.expire_all()
        services = await self.session.scalar(
            select(func.count()).select_from(UserService).where(UserService.remark == f"order:{receipt.order_id}")
        )
        self.assertEqual(services, 1)
        self.stuck_notice.assert_awaited_once()

    async def test_a_payment_that_never_got_approved_keeps_the_plain_error(self) -> None:
        receipt = await self.receipt_payment()
        with patch(
            "app.services.payment_review_actions.approve_payment", AsyncMock(side_effect=ValueError("این پرداخت قابل تأیید نیست"))
        ):
            result = await self.approve(receipt)
        self.assertTrue(result.alert_fa.startswith("خطا:"))
        self.assertEqual(len(await self.failures(receipt)), 0)
        self.stuck_notice.assert_not_awaited()
        self.buyer_notice.assert_not_awaited()


class PlatformManualApproveTests(ManualApproveCases, UndeliveredPaidOrderTestCase):
    shop_id = None


class ShopManualApproveTests(ManualApproveCases, UndeliveredPaidOrderTestCase):
    shop_id = SHOP
