"""Money/delivery safety: wallet top-up, service delete, Stars refund, loyalty keys."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("WEB_SECRET", "test-web-secret-for-money-phase-32chars")

ROOT = Path(__file__).resolve().parents[1]


class LoyaltyIdempotencyKeyTests(unittest.TestCase):
    def test_handlers_do_not_use_callback_id_as_idempotency_key(self):
        src = (ROOT / "app/bot/handlers/loyalty.py").read_text(encoding="utf-8")
        self.assertIn('f"tg:wheel:{db_user.id}:{spins_done}"', src)
        # Double-tap must not mint unique keys via Telegram callback.id
        self.assertNotIn("tg:{callback.id}", src)
        self.assertNotIn("tg_wheel:{callback.id}", src)


class RedemptionSlotKeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_double_tap_shares_the_key_and_a_new_redemption_moves_it(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.db.models import Base, BotUser, RewardRedemption
        from app.services.loyalty import next_redemption_key

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add(BotUser(id=1, telegram_id=1, referral_code="a"))
            await session.commit()
            first = await next_redemption_key(session, 1, 7, None)
            self.assertEqual(first, await next_redemption_key(session, 1, 7, None))
            self.assertNotEqual(first, await next_redemption_key(session, 1, 7, 5), "another service is another slot")
            session.add(RewardRedemption(user_id=1, reward_id=7, points_spent=1, reward_type="wallet_credit",
                                         reward_value=1, status="completed", idempotency_key=first))
            await session.commit()
            self.assertNotEqual(first, await next_redemption_key(session, 1, 7, None))
        await engine.dispose()


class ServiceDeleteFailClosedTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_row_kept_when_pg_delete_and_disable_fail(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.db.models import Base, BotUser, Role, UserService
        from app.services.bot_user_admin import admin_delete_service

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async with Session() as session:
            user = BotUser(telegram_id=9001, referral_code="U9001", role=Role.USER.value)
            session.add(user)
            await session.flush()
            svc = UserService(
                bot_user_id=user.id,
                pg_user_id=4242,
                pg_username="u4242",
                remark="test",
            )
            session.add(svc)
            await session.commit()
            await session.refresh(svc)

            pg = MagicMock()
            pg.delete_user_by_id = AsyncMock(side_effect=RuntimeError("pg down"))
            pg.set_disabled_by_id = AsyncMock(side_effect=RuntimeError("pg down"))
            staff = {"role": "admin", "pg_permissions": ["users.delete", "users.disable"]}

            with (
                patch(
                    "app.services.bot_user_admin._pg_client_for_bot_service",
                    AsyncMock(return_value=pg),
                ),
                patch(
                    "app.services.bot_user_admin.assert_staff_pg_user_action",
                    MagicMock(),
                ),
            ):
                with self.assertRaises(ValueError) as ctx:
                    await admin_delete_service(session, svc, delete_pg=True, staff=staff)
            self.assertIn("پاسارگارد", str(ctx.exception))

            still = await session.get(UserService, svc.id)
            self.assertIsNotNone(still)
            self.assertEqual(still.pg_user_id, 4242)

        await engine.dispose()


class WalletTopupApproveTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_user_rolls_back_approved_claim(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.db.models import Base, BotUser, Payment, PaymentMethod, PaymentStatus, Role
        from app.services.orders import approve_payment

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async with Session() as session:
            # Create then delete the user so payment.user_id points nowhere
            user = BotUser(telegram_id=9101, referral_code="W9101", role=Role.USER.value)
            session.add(user)
            await session.flush()
            orphan_uid = int(user.id)
            pay = Payment(
                user_id=orphan_uid,
                amount=50000,
                method=PaymentMethod.CARD.value,
                status=PaymentStatus.PENDING.value,
                is_wallet_topup=True,
            )
            session.add(pay)
            await session.commit()
            await session.refresh(pay)

            await session.delete(user)
            await session.commit()
            await session.refresh(pay)

            with self.assertRaises(ValueError) as ctx:
                await approve_payment(session, pay, reviewer_tg=1)
            self.assertIn("یافت نشد", str(ctx.exception))

            await session.refresh(pay)
            self.assertEqual(pay.status, PaymentStatus.PENDING.value)
            self.assertIn("wallet topup blocked", pay.review_note or "")

        await engine.dispose()


class StarsRefundHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_refund_helper_calls_telegram_api(self):
        from app.bot.handlers.payments import _refund_stars_charge

        bot = MagicMock()
        bot.refund_star_payment = AsyncMock(return_value=True)
        ok = await _refund_stars_charge(
            bot, telegram_user_id=55, charge_id="chg_abc"
        )
        self.assertTrue(ok)
        bot.refund_star_payment.assert_awaited_once_with(
            user_id=55, telegram_payment_charge_id="chg_abc"
        )

    async def test_mismatch_path_refunds_and_rejects(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.bot.handlers import payments as paymod
        from app.db.models import Base, BotUser, Payment, PaymentMethod, PaymentStatus, Role

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async with Session() as session:
            user = BotUser(telegram_id=9201, referral_code="S9201", role=Role.USER.value)
            session.add(user)
            await session.flush()
            payment = Payment(
                user_id=user.id,
                amount=10000,
                method=PaymentMethod.STARS.value,
                status=PaymentStatus.PENDING.value,
            )
            session.add(payment)
            await session.commit()
            await session.refresh(payment)

            bot = MagicMock()
            bot.refund_star_payment = AsyncMock(return_value=True)
            bot.send_message = AsyncMock()
            message = MagicMock()
            message.bot = bot
            message.answer = AsyncMock()
            message.successful_payment = SimpleNamespace(
                currency="XTR",
                total_amount=7,
                telegram_payment_charge_id="chg_mismatch",
                invoice_payload=f"stars:{payment.id}:9",
            )

            await paymod.stars_successful_payment(message, session, user)
            await session.refresh(payment)
            self.assertEqual(payment.status, PaymentStatus.REJECTED.value)
            self.assertIn("mismatch", payment.review_note or "")
            self.assertIn("refunded", payment.review_note or "")
            bot.refund_star_payment.assert_awaited()
            message.answer.assert_awaited()
            answered = message.answer.await_args.args[0]
            self.assertIn("بازگردانده", answered)

        await engine.dispose()


class SourceWiringTests(unittest.TestCase):
    def test_service_delete_fail_closed_in_source(self):
        src = (ROOT / "app/services/bot_user_admin.py").read_text(encoding="utf-8")
        self.assertIn("سرویس محلی حذف نشد", src)
        # Confirmed DELETE+GET 404 is the only idempotent escape; transport stays closed.
        self.assertIn("async def ensure_pg_user_removed_or_absent", src)
        self.assertIn("async def _pg_user_presence", src)
        self.assertIn('presence == "absent"', src)
        self.assertIn("is_pg_not_found", src)

    def test_wallet_topup_missing_user_guard_in_source(self):
        src = (ROOT / "app/services/orders.py").read_text(encoding="utf-8")
        self.assertIn("wallet topup blocked: bot user missing", src)
        self.assertIn("کاربر پرداخت‌کننده برای شارژ کیف پول یافت نشد", src)

    def test_stars_refund_helper_wired(self):
        src = (ROOT / "app/bot/handlers/payments.py").read_text(encoding="utf-8")
        self.assertIn("async def _refund_stars_charge", src)
        self.assertIn("refund_star_payment", src)
        self.assertIn("stars amount mismatch", src)


if __name__ == "__main__":
    unittest.main()
