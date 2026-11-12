"""Customer messages that tell the customer to ask for help carry the support button."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, BotUser, Order, Payment, PaymentStatus
from app.services import payment_voids
from app.services.delivery import build_delivery_content
from app.services.payer_notify import notify_payer
from app.services.notice_links import opened_screen
from app.services.support_button import SUPPORT_HOME_CALLBACK, SUPPORT_NOTICE_CALLBACK, support_markup, with_support_row


def callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class SupportButtonTests(unittest.TestCase):
    def test_the_button_opens_the_support_home(self) -> None:
        self.assertEqual(callbacks(support_markup()), [SUPPORT_NOTICE_CALLBACK])
        self.assertEqual(opened_screen(SUPPORT_NOTICE_CALLBACK), SUPPORT_HOME_CALLBACK)

    def test_a_support_row_is_appended_once(self) -> None:
        base = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="x", callback_data="x:1")]])
        once = with_support_row(base)
        self.assertEqual(callbacks(once), ["x:1", SUPPORT_NOTICE_CALLBACK])
        self.assertEqual(callbacks(with_support_row(once)), ["x:1", SUPPORT_NOTICE_CALLBACK])
        self.assertEqual(callbacks(with_support_row(None)), [SUPPORT_NOTICE_CALLBACK])


class NotifyPayerMarkupTests(unittest.IsolatedAsyncioTestCase):
    async def send(self, **kwargs):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        user = SimpleNamespace(telegram_id=555)
        session = MagicMock()
        session.get = AsyncMock(return_value=user)
        with patch("app.services.reseller_bots.open_notify_bot_for_user", AsyncMock(return_value=(bot, False))):
            ok = await notify_payer(session, 7, title="t", body="b", **kwargs)
        self.assertTrue(ok)
        return bot.send_message.await_args.kwargs

    async def test_the_markup_is_sent_with_the_message(self) -> None:
        sent = await self.send(reply_markup=support_markup())
        self.assertEqual(callbacks(sent["reply_markup"]), [SUPPORT_NOTICE_CALLBACK])

    async def test_without_markup_nothing_is_attached(self) -> None:
        self.assertNotIn("reply_markup", await self.send())


class VoidNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_void_notice_offers_support(self) -> None:
        payment = Payment(id=5, user_id=7, amount=1000, is_wallet_topup=True)
        session = MagicMock()
        session.get = AsyncMock(return_value=payment)
        void = SimpleNamespace(refund_amount=0, shop_id=None, status="done")
        with patch.object(payment_voids, "list_voids", AsyncMock(return_value={})), \
                patch.object(payment_voids, "void_approved_payment", AsyncMock(return_value=void)), \
                patch.object(payment_voids, "notify_payer", AsyncMock(return_value=True)) as notify:
            await payment_voids.void_and_notify(session, 5, shop_id=None, actor="a", reason="r")
        notify.assert_awaited_once()
        self.assertEqual(callbacks(notify.await_args.kwargs["reply_markup"]), [SUPPORT_NOTICE_CALLBACK])


class BotRejectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_rejected_buyer_gets_the_notice_with_the_support_button(self) -> None:
        from app.bot.handlers import payments

        payment = Payment(id=9, user_id=7, amount=10)
        buyer = BotUser(id=7, telegram_id=777, referral_code="b", role="user")
        session = MagicMock()
        session.get = AsyncMock(return_value=buyer)
        callback = AsyncMock()
        callback.data = "payrev:no:9"
        callback.bot = MagicMock()
        callback.bot.send_message = AsyncMock()
        with patch.object(payments, "_payrev_load", AsyncMock(return_value=payment)), \
                patch.object(payments, "reject_payment", AsyncMock()), \
                patch.object(payments, "get_all_settings", AsyncMock(return_value={})):
            await payments.pay_reject(callback, session=session, db_user=SimpleNamespace(telegram_id=1))
        callback.bot.send_message.assert_awaited_once()
        sent = callback.bot.send_message.await_args
        self.assertEqual(sent.args[0], 777)
        self.assertEqual(callbacks(sent.kwargs["reply_markup"]), [SUPPORT_NOTICE_CALLBACK])
        self.assertIn("پشتیبانی", sent.args[1])


class UndeliveredNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_paid_order_without_a_service_asks_to_use_support(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session = async_sessionmaker(engine, expire_on_commit=False)()
        session.add(BotUser(id=1, telegram_id=11, referral_code="b", role="user"))
        await session.flush()
        order = Order(id=3, user_id=1, amount=10, status="paid")
        payment = Payment(id=4, user_id=1, order_id=3, amount=10, method="card", status=PaymentStatus.APPROVED.value)
        session.add_all([order, payment])
        await session.commit()
        try:
            content = await build_delivery_content(session, payment, order)
        finally:
            await session.close()
            await engine.dispose()
        self.assertTrue(content.get("needs_support"))


if __name__ == "__main__":
    unittest.main()
