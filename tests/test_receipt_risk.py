"""Receipt risk label: reasons from the customer's history in the same shop, and the auto-approve hold."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Base, BotUser, Order, Payment, PaymentStatus, Plan
from app.services import receipt_risk as risk
from app.services.receipts import process_receipt, submit_receipt
from app.services.users import clear_settings_cache, set_setting
from tests.postgres_support import scoped_async_engine

NOW = datetime.now(timezone.utc)
SHOP_A, SHOP_B, BUYER = 5, 6, 9


class ClassifyTests(unittest.TestCase):
    def test_a_strong_reason_or_two_reasons_need_review(self) -> None:
        self.assertEqual(risk.classify([]).level, risk.LEVEL_LOW)
        self.assertEqual(risk.classify([risk.REASON_NEW]).level, risk.LEVEL_LOW)
        self.assertTrue(risk.classify([risk.REASON_BURST]).needs_review)
        self.assertTrue(risk.classify([risk.REASON_NEW, risk.REASON_DUPLICATE]).needs_review)

    def test_the_note_lists_every_reason(self) -> None:
        note = risk.risk_note(risk.classify([risk.REASON_REJECTED, risk.REASON_NEW]))
        self.assertIn("بررسی دقیق", note)
        self.assertIn(risk.REASON_REJECTED, note)
        self.assertIn("کم‌ریسک", risk.risk_note(risk.classify([])))


class AssessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP_A, telegram_id=5, referral_code="a", role="reseller"),
                BotUser(id=SHOP_B, telegram_id=6, referral_code="b", role="reseller"),
                BotUser(id=BUYER, telegram_id=9, referral_code="c"),
                Plan(id=1, name="p", price=100_000),
            ])
            await session.commit()
        clear_settings_cache()

    async def asyncTearDown(self) -> None:
        clear_settings_cache()
        await self.engine.dispose()

    async def payment(self, *, shop: int | None = SHOP_A, status: str = PaymentStatus.PENDING.value,
                      receipt_at: datetime | None = None, topup: bool = False) -> Payment:
        async with self.Session() as session:
            order_id = None
            if not topup:
                order = Order(user_id=BUYER, plan_id=1, amount=100_000, status="awaiting_approval", reseller_id=shop)
                session.add(order)
                await session.flush()
                order_id = order.id
            payment = Payment(user_id=BUYER, order_id=order_id, amount=100_000, method="card", status=status,
                              receipt_at=receipt_at, is_wallet_topup=topup, wallet_shop_id=shop if topup else None)
            session.add(payment)
            await session.commit()
            return payment

    async def assess(self, payment: Payment, *, shop: int | None = SHOP_A, duplicates: int = 0) -> risk.ReceiptRisk:
        async with self.Session() as session:
            return await risk.assess_receipt(session, payment, shop_id=shop, duplicates=duplicates, now=NOW)

    async def test_a_new_customer_is_low_risk_with_the_reason_shown(self) -> None:
        result = await self.assess(await self.payment())
        self.assertEqual((result.level, result.reasons), (risk.LEVEL_LOW, (risk.REASON_NEW,)))

    async def test_a_returning_customer_has_no_reason(self) -> None:
        await self.payment(status=PaymentStatus.APPROVED.value, topup=True)
        self.assertEqual((await self.assess(await self.payment())).reasons, ())

    async def test_a_recent_rejection_needs_review_only_in_its_own_shop(self) -> None:
        await self.payment(status=PaymentStatus.APPROVED.value)
        await self.payment(shop=SHOP_B, status=PaymentStatus.REJECTED.value)
        current = await self.payment()
        self.assertFalse((await self.assess(current)).needs_review)
        await self.payment(status=PaymentStatus.REJECTED.value)
        self.assertEqual((await self.assess(current)).reasons, (risk.REASON_REJECTED,))

    async def test_a_burst_of_receipts_and_a_reused_image_need_review(self) -> None:
        await self.payment(status=PaymentStatus.APPROVED.value)
        for _ in range(2):
            await self.payment(receipt_at=NOW - timedelta(minutes=5))
        current = await self.payment(receipt_at=NOW)
        self.assertIn(risk.REASON_BURST, (await self.assess(current)).reasons)
        self.assertIn(risk.REASON_DUPLICATE, (await self.assess(current, duplicates=1)).reasons)

    async def test_the_platform_scope_ignores_shop_history(self) -> None:
        await self.payment(status=PaymentStatus.REJECTED.value)
        result = await self.assess(await self.payment(shop=None), shop=None)
        self.assertEqual(result.reasons, (risk.REASON_NEW,))

    async def test_a_risky_receipt_waits_for_a_person_only_when_the_shop_asks(self) -> None:
        current = await self.payment()
        risky = risk.classify([risk.REASON_BURST])
        async with self.Session() as session:
            await set_setting(session, "auto_approve_payments", "1", reseller_id=SHOP_A)
            await session.commit()
        for hold, expect_auto in (("0", True), ("1", False)):
            clear_settings_cache()
            async with self.Session() as session:
                await set_setting(session, risk.HOLD_KEY, hold, reseller_id=SHOP_A)
                await session.commit()
            auto, pending = AsyncMock(return_value=None), AsyncMock()
            with patch("app.services.receipts.auto_approve_receipt", auto), \
                    patch("app.services.receipts.notify_pending_approval", pending), \
                    patch("app.services.receipts._waits_for_schedule", AsyncMock(return_value=False)):
                async with self.Session() as session:
                    await process_receipt(session, await session.get(Payment, current.id), bot=AsyncMock(),
                                          user_tg_id=9, risk=risky)
            self.assertEqual((auto.await_count, pending.await_count), (int(expect_auto), int(not expect_auto)), hold)

    async def test_the_reviewer_note_carries_the_label(self) -> None:
        current = await self.payment()
        review = AsyncMock(return_value="ok")
        with patch("app.services.receipts.download_receipt_sha256", AsyncMock(return_value="ab" * 32)), \
                patch("app.services.receipts.process_receipt", review):
            async with self.Session() as session:
                await submit_receipt(session, await session.get(Payment, current.id), bot=AsyncMock(), file_id="F")
        kwargs = review.await_args.kwargs
        self.assertIn("ریسک رسید", kwargs["reviewer_note"])
        self.assertEqual(kwargs["risk"].reasons, (risk.REASON_NEW,))
        async with self.Session() as session:
            await set_setting(session, risk.ENABLED_KEY, "0", reseller_id=SHOP_A)
            await session.commit()
        clear_settings_cache()
        second = await self.payment()
        with patch("app.services.receipts.download_receipt_sha256", AsyncMock(return_value="cd" * 32)), \
                patch("app.services.receipts.process_receipt", review):
            async with self.Session() as session:
                await submit_receipt(session, await session.get(Payment, second.id), bot=AsyncMock(), file_id="G")
        self.assertIsNone(review.await_args.kwargs["risk"])


if __name__ == "__main__":
    unittest.main()
