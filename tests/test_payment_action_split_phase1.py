"""Phase 1 — separate approve / resume / resend payment-review actions."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker
from tests.postgres_support import scoped_async_engine


class Phase1DiagActionMappingTests(unittest.IsolatedAsyncioTestCase):
    async def _session(self):
        from app.db.models import Base

        engine = scoped_async_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        return engine, Session

    async def test_pending_prefers_approve_callback(self):
        from app.db.models import BotUser, Payment, PaymentMethod, PaymentStatus, Role
        from app.services.payment_review_diag import diagnose_payment_review

        engine, Session = await self._session()
        async with Session() as session:
            user = BotUser(telegram_id=9101, referral_code="A9101", role=Role.USER.value)
            session.add(user)
            await session.flush()
            pay = Payment(
                user_id=user.id,
                amount=1000,
                method=PaymentMethod.CARD.value,
                status=PaymentStatus.PENDING.value,
                is_wallet_topup=True,
            )
            session.add(pay)
            await session.commit()
            await session.refresh(pay)
            diag = await diagnose_payment_review(session, pay)
            self.assertEqual(diag.preferred_action, "approve")
            self.assertEqual(diag.callback_data(), f"payrev:ok:{pay.id}")
            self.assertTrue(diag.allows_action("approve"))
            self.assertFalse(diag.allows_action("resume"))
            self.assertFalse(diag.allows_action("resend"))
        await engine.dispose()

    async def test_fulfill_prefers_resume_go(self):
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
        from app.services.payment_review_diag import diagnose_payment_review

        engine, Session = await self._session()
        async with Session() as session:
            user = BotUser(telegram_id=9102, referral_code="A9102", role=Role.USER.value)
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
            )
            session.add(pay)
            await session.commit()
            await session.refresh(pay)
            await session.refresh(order)
            diag = await diagnose_payment_review(session, pay, order=order)
            self.assertEqual(diag.preferred_action, "resume")
            self.assertEqual(diag.callback_data(), f"payrev:go:{pay.id}")
            self.assertEqual(diag.action_label_fa, "ادامه تحویل")
            self.assertIn("تأیید پرداخت", diag.wrong_tool_alert("approve"))
        await engine.dispose()

    async def test_resend_prefers_send(self):
        from app.db.models import (
            BotUser,
            DeliveryFailure,
            Order,
            OrderStatus,
            Payment,
            PaymentMethod,
            PaymentStatus,
            Plan,
            Role,
            UserService,
        )
        from app.services.payment_review_diag import diagnose_payment_review

        engine, Session = await self._session()
        async with Session() as session:
            user = BotUser(telegram_id=9103, referral_code="A9103", role=Role.USER.value)
            plan = Plan(name="P3", price=5000, data_limit_gb=5, duration_days=30)
            session.add_all([user, plan])
            await session.flush()
            svc = UserService(
                bot_user_id=user.id,
                plan_id=plan.id,
                pg_username="u9103",
                remark="order:z",
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
                review_note="delivery_failed",
            )
            session.add(pay)
            await session.flush()
            session.add(
                DeliveryFailure(
                    order_id=order.id,
                    payment_id=pay.id,
                    error="tg fail",
                    attempts=1,
                )
            )
            await session.commit()
            await session.refresh(pay)
            await session.refresh(order)
            diag = await diagnose_payment_review(session, pay, order=order)
            self.assertEqual(diag.preferred_action, "resend")
            self.assertEqual(diag.callback_data(), f"payrev:send:{pay.id}")
            self.assertFalse(diag.allows_action("approve"))
            self.assertFalse(diag.allows_action("resume"))
        await engine.dispose()

    async def test_resend_action_refuses_when_complete(self):
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
        from app.services.payment_review_actions import execute_payment_resend

        engine, Session = await self._session()
        async with Session() as session:
            user = BotUser(telegram_id=9104, referral_code="A9104", role=Role.USER.value)
            plan = Plan(name="P4", price=5000, data_limit_gb=5, duration_days=30)
            session.add_all([user, plan])
            await session.flush()
            svc = UserService(
                bot_user_id=user.id,
                plan_id=plan.id,
                pg_username="u9104",
                remark="order:w",
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

            bot = MagicMock()
            result = await execute_payment_resend(
                session, pay, reviewer_tg=1, bot=bot
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.error, "wrong_tool")
            self.assertEqual(result.diag.kind, "approved_complete")
        await engine.dispose()

    async def test_resume_action_calls_approve_payment(self):
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
        from app.services import payment_review_actions as pra

        engine, Session = await self._session()
        async with Session() as session:
            user = BotUser(telegram_id=9105, referral_code="A9105", role=Role.USER.value)
            plan = Plan(name="P5", price=8000, data_limit_gb=8, duration_days=30)
            session.add_all([user, plan])
            await session.flush()
            order = Order(
                user_id=user.id,
                plan_id=plan.id,
                amount=8000,
                status=OrderStatus.PAID.value,
                payment_method=PaymentMethod.CARD.value,
            )
            session.add(order)
            await session.flush()
            pay = Payment(
                order_id=order.id,
                user_id=user.id,
                amount=8000,
                method=PaymentMethod.CARD.value,
                status=PaymentStatus.APPROVED.value,
                reviewed_by=1,
            )
            session.add(pay)
            await session.commit()
            await session.refresh(pay)
            await session.refresh(order)

            delivered = Order(
                id=order.id,
                user_id=user.id,
                plan_id=plan.id,
                amount=8000,
                status=OrderStatus.DELIVERED.value,
                payment_method=PaymentMethod.CARD.value,
                service_id=1,
            )
            bot = MagicMock()
            with patch.object(
                pra, "approve_payment", new=AsyncMock(return_value=delivered)
            ) as ap, patch.object(
                pra, "_deliver_and_notify", new=AsyncMock(return_value=None)
            ):
                result = await pra.execute_payment_resume(
                    session, pay, reviewer_tg=42, bot=bot
                )
            ap.assert_awaited_once()
            self.assertTrue(result.ok)
            self.assertEqual(result.action, "resume")
            self.assertIn("ادامه", result.alert_fa)
        await engine.dispose()


class Phase1WiringStaticTests(unittest.TestCase):

    def test_keyboards_split_actions(self):
        from app.bot.keyboards import payment_action_markup, payment_resend, payment_resume, payment_review

        ok = payment_review(7)
        self.assertEqual(ok.inline_keyboard[0][0].callback_data, "payrev:ok:7")
        self.assertEqual(ok.inline_keyboard[0][1].callback_data, "payrev:no:7")

        go = payment_resume(8)
        self.assertEqual(go.inline_keyboard[0][0].callback_data, "payrev:go:8")
        self.assertEqual(len(go.inline_keyboard[0]), 1)

        send = payment_resend(9)
        self.assertEqual(send.inline_keyboard[0][0].callback_data, "payrev:send:9")

        via = payment_action_markup(10, action="resend")
        self.assertEqual(via.inline_keyboard[0][0].callback_data, "payrev:send:10")


if __name__ == "__main__":
    unittest.main()
