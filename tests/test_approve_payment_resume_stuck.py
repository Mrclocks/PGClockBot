"""Approve must resume incomplete APPROVED payments — never false «قبلاً تأیید شده»."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class ApprovePaymentResumeTests(unittest.IsolatedAsyncioTestCase):
    async def _session(self):
        from app.db.models import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        return engine, Session

    async def test_approved_paid_order_resumes_fulfill_not_false_confirmed(self):
        from app.db.models import (
            BotUser,
            Order,
            OrderStatus,
            Payment,
            PaymentMethod,
            PaymentStatus,
            Plan,
            Role,
        )
        from app.services.orders import approve_payment

        engine, Session = await self._session()
        async with Session() as session:
            user = BotUser(telegram_id=7001, referral_code="R7001", role=Role.USER.value)
            plan = Plan(name="P", price=10000, data_limit_gb=10, duration_days=30)
            session.add_all([user, plan])
            await session.flush()
            order = Order(
                user_id=user.id,
                plan_id=plan.id,
                amount=10000,
                status=OrderStatus.PAID.value,
                payment_method=PaymentMethod.CARD.value,
            )
            session.add(order)
            await session.flush()
            pay = Payment(
                order_id=order.id,
                user_id=user.id,
                amount=10000,
                method=PaymentMethod.CARD.value,
                status=PaymentStatus.APPROVED.value,
                reviewed_by=1,
                receipt_file_id="file:abc",
            )
            session.add(pay)
            await session.commit()
            await session.refresh(pay)
            await session.refresh(order)

            delivered = Order(
                id=order.id,
                user_id=user.id,
                plan_id=plan.id,
                amount=10000,
                status=OrderStatus.DELIVERED.value,
                payment_method=PaymentMethod.CARD.value,
                service_id=99,
            )
            with patch(
                "app.services.orders.fulfill_paid_order",
                new=AsyncMock(return_value=delivered),
            ) as fulfill:
                result = await approve_payment(session, pay, reviewer_tg=42)
            fulfill.assert_awaited_once()
            self.assertEqual(result.status, OrderStatus.DELIVERED.value)
            await session.refresh(pay)
            self.assertEqual(pay.status, PaymentStatus.APPROVED.value)

        await engine.dispose()

    async def test_fully_delivered_still_rejects_as_already_confirmed(self):
        from app.db.models import (
            BotUser,
            Order,
            OrderStatus,
            Payment,
            PaymentMethod,
            PaymentStatus,
            Plan,
            Role,
            UserService,
        )
        from app.services.orders import approve_payment

        engine, Session = await self._session()
        async with Session() as session:
            user = BotUser(telegram_id=7002, referral_code="R7002", role=Role.USER.value)
            plan = Plan(name="P2", price=5000, data_limit_gb=5, duration_days=30)
            session.add_all([user, plan])
            await session.flush()
            svc = UserService(
                bot_user_id=user.id,
                plan_id=plan.id,
                pg_username="u7002",
                remark="order:1",
            )
            session.add(svc)
            await session.flush()
            order = Order(
                user_id=user.id,
                plan_id=plan.id,
                amount=5000,
                status=OrderStatus.DELIVERED.value,
                payment_method=PaymentMethod.CARD.value,
                service_id=svc.id,
            )
            session.add(order)
            await session.flush()
            pay = Payment(
                order_id=order.id,
                user_id=user.id,
                amount=5000,
                method=PaymentMethod.CARD.value,
                status=PaymentStatus.APPROVED.value,
                reviewed_by=1,
            )
            session.add(pay)
            await session.commit()
            await session.refresh(pay)

            with self.assertRaises(ValueError) as ctx:
                await approve_payment(session, pay, reviewer_tg=1)
            self.assertIn("قبلاً تأیید شده", str(ctx.exception))

        await engine.dispose()

    async def test_wallet_topup_resume_credits_once_only(self):
        from app.db.models import (
            BotUser,
            Payment,
            PaymentMethod,
            PaymentStatus,
            Role,
            WalletTransaction,
        )
        from app.services.orders import (
            _wallet_topup_credit_reason,
            approve_payment,
        )

        engine, Session = await self._session()
        async with Session() as session:
            user = BotUser(
                telegram_id=7003,
                referral_code="R7003",
                role=Role.USER.value,
                wallet_balance=0,
            )
            session.add(user)
            await session.flush()
            pay = Payment(
                user_id=user.id,
                amount=25000,
                method=PaymentMethod.CARD.value,
                status=PaymentStatus.APPROVED.value,
                is_wallet_topup=True,
                reviewed_by=1,
            )
            session.add(pay)
            await session.commit()
            await session.refresh(pay)

            # First resume credits
            out = await approve_payment(session, pay, reviewer_tg=1)
            self.assertIsNone(out)
            await session.refresh(user)
            self.assertEqual(user.wallet_balance, 25000)

            # Second resume is idempotent — no double credit
            out2 = await approve_payment(session, pay, reviewer_tg=1)
            self.assertIsNone(out2)
            await session.refresh(user)
            self.assertEqual(user.wallet_balance, 25000)

            txs = (
                await session.execute(
                    WalletTransaction.__table__.select().where(
                        WalletTransaction.user_id == user.id,
                        WalletTransaction.reason
                        == _wallet_topup_credit_reason(int(pay.id)),
                    )
                )
            ).fetchall()
            self.assertEqual(len(txs), 1)

        await engine.dispose()

    async def test_process_receipt_auto_approve_partial_does_not_lie(self):
        """When approve commits but delivery fails, user text must not imply only pending."""
        from pathlib import Path

        src = Path("app/services/receipts.py").read_text(encoding="utf-8")
        self.assertIn("notify_approved_delivery_stuck", src)
        self.assertIn("PaymentStatus.APPROVED.value", src)
        self.assertIn("pending_buyer_note", src)


class NotifyStuckMarkupTests(unittest.TestCase):
    def test_stuck_notify_has_retry_not_reject(self):
        src = open("app/services/notifications.py", encoding="utf-8").read()
        self.assertIn("notify_approved_delivery_stuck", src)
        # Phase 1: go/send via payment_action_markup — never reject
        start = src.index("async def notify_approved_delivery_stuck")
        end = src.index("\ndef build_qr_caption", start)
        body = src[start:end]
        self.assertIn("payment_action_markup", body)
        self.assertIn("diagnose_payment_review", body)
        self.assertNotIn("payrev:ok:", body)
        self.assertNotIn("payrev:no:", body)


if __name__ == "__main__":
    unittest.main()
