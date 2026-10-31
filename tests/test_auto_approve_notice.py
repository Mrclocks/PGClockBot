"""The automatic-approval notice carries the receipt photo and buttons to the customer and service."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, Order, Payment, Plan, Role, UserService
from app.services import notifications
from app.services.notifications import _customer_markup, _receipt_photo, notify_auto_approve

FILE_ID = "AgACAgQAAxkBAAIBQ2Z-receiptphotofileid"
CALLBACK_LIMIT = 64


def _callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class NoticeTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'n.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _seed(self, session, *, shop: int | None = None, name: str = "Sara", note: str | None = None):
        user = BotUser(telegram_id=501, username="sara", full_name=name, role=Role.USER.value, referral_code="R501")
        plan = Plan(name="۳۰ روزه", price=90000, duration_days=30, data_limit_gb=50)
        session.add_all([user, plan])
        await session.flush()
        order = Order(user_id=user.id, plan_id=plan.id, reseller_id=shop, amount=90000, status="delivered", note=note)
        session.add(order)
        await session.flush()
        payment = Payment(order_id=order.id, user_id=user.id, amount=90000, status="approved", receipt_file_id=FILE_ID)
        session.add(payment)
        await session.commit()
        return user, order, payment

    async def _notify(self, session, payment) -> AsyncMock:
        dispatch = AsyncMock(return_value=1)
        with patch.object(notifications, "_dispatch_dual_notify", dispatch):
            await notify_auto_approve(AsyncMock(), session, payment)
        return dispatch


class ReceiptPhotoTests(unittest.TestCase):
    def test_only_a_telegram_file_id_is_sent_as_a_photo(self) -> None:
        self.assertEqual(_receipt_photo(Payment(receipt_file_id=FILE_ID)), FILE_ID)
        for marker in (None, "", "  ", "stars:charge_1", "card_auto:awaiting:7"):
            with self.subTest(marker=marker):
                self.assertIsNone(_receipt_photo(Payment(receipt_file_id=marker)))


class CustomerMarkupTests(unittest.TestCase):
    def test_the_platform_bot_gets_the_customer_and_the_service_card(self) -> None:
        markup = _customer_markup(user_id=7, service_id=9, shop_id=None)
        self.assertEqual(_callbacks(markup), ["adm:users:view:7", "adm:users:svc:7:9"])

    def test_a_shop_bot_only_gets_the_shop_customer_card(self) -> None:
        markup = _customer_markup(user_id=7, service_id=9, shop_id=3)
        self.assertEqual(_callbacks(markup), ["res:user:7"])

    def test_no_service_button_without_a_service_and_nothing_without_a_user(self) -> None:
        self.assertEqual(_callbacks(_customer_markup(user_id=7, service_id=None, shop_id=None)), ["adm:users:view:7"])
        self.assertIsNone(_customer_markup(user_id=None, service_id=9, shop_id=None))

    def test_callback_data_fits_telegrams_limit_for_large_ids(self) -> None:
        markup = _customer_markup(user_id=2**31 - 1, service_id=2**31 - 1, shop_id=None)
        for data in _callbacks(markup):
            self.assertLessEqual(len(data.encode()), CALLBACK_LIMIT)


class AutoApproveNoticeTests(NoticeTestCase):
    async def test_the_notice_carries_the_receipt_photo_and_the_payment_details(self) -> None:
        async with self.Session() as session:
            user, order, payment = await self._seed(session)
            dispatch = await self._notify(session, payment)
        args, kwargs = dispatch.await_args
        text = args[3]
        self.assertEqual(kwargs["photo"], FILE_ID)
        self.assertIs(kwargs["payment"], payment)
        for expected in ("تأیید خودکار", f"#{payment.id}", "Sara", "خودکار تأیید شد"):
            self.assertIn(expected, text)
        self.assertEqual(args[2], "notify_auto_approve")

    async def test_the_service_button_follows_the_service_delivery_created_later(self) -> None:
        async with self.Session() as session:
            user, order, payment = await self._seed(session)
            await session.get(Order, order.id)  # loaded before delivery, as in process_receipt
            async with self.Session() as other:
                service = UserService(bot_user_id=user.id, pg_username="s1", remark=f"order:{order.id}")
                other.add(service)
                await other.flush()
                (await other.get(Order, order.id)).service_id = service.id
                await other.commit()
            dispatch = await self._notify(session, payment)
        self.assertEqual(_callbacks(dispatch.await_args.kwargs["markup"]), [f"adm:users:view:{user.id}", f"adm:users:svc:{user.id}:{service.id}"])

    async def test_a_shop_payment_is_routed_to_the_shop_with_the_shop_buttons(self) -> None:
        async with self.Session() as session:
            shop = BotUser(telegram_id=900, username="shop", full_name="Shop", role=Role.RESELLER.value, referral_code="R900")
            session.add(shop)
            await session.flush()
            user, order, payment = await self._seed(session, shop=shop.id)
            dispatch = await self._notify(session, payment)
        self.assertEqual(_callbacks(dispatch.await_args.kwargs["markup"]), [f"res:user:{user.id}"])

    async def test_a_wallet_top_up_has_no_service_button(self) -> None:
        async with self.Session() as session:
            user, order, payment = await self._seed(session)
            payment.order_id = None
            payment.is_wallet_topup = True
            await session.commit()
            dispatch = await self._notify(session, payment)
        self.assertEqual(_callbacks(dispatch.await_args.kwargs["markup"]), [f"adm:users:view:{user.id}"])
        self.assertIn("شارژ کیف پول", dispatch.await_args.args[3])

    async def test_an_agency_fee_has_no_service_button(self) -> None:
        async with self.Session() as session:
            user, order, payment = await self._seed(session, note="reseller_app:4")
            order.service_id = None
            await session.commit()
            dispatch = await self._notify(session, payment)
        self.assertEqual(_callbacks(dispatch.await_args.kwargs["markup"]), [f"adm:users:view:{user.id}"])

    async def test_a_hostile_customer_name_cannot_break_the_staff_message(self) -> None:
        async with self.Session() as session:
            user, order, payment = await self._seed(session, name="<b>x</b><a href='evil'>")
            dispatch = await self._notify(session, payment)
        text = dispatch.await_args.args[3]
        self.assertNotIn("<a href='evil'>", text)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)

    async def test_a_receipt_marker_is_not_sent_as_a_photo(self) -> None:
        async with self.Session() as session:
            user, order, payment = await self._seed(session)
            payment.receipt_file_id = "card_auto:awaiting:5"
            await session.commit()
            dispatch = await self._notify(session, payment)
        self.assertIsNone(dispatch.await_args.kwargs["photo"])


class DeliveryToStaffTests(NoticeTestCase):
    """Through the real fan-out to the platform admin chat, with a recording bot."""

    ADMIN_CHAT = 111

    def _bot(self, *, photo_works: bool = True) -> AsyncMock:
        bot = AsyncMock()
        reply = SimpleNamespace(message_id=5, chat=SimpleNamespace(id=self.ADMIN_CHAT))
        bot.send_message.return_value = reply
        if photo_works:
            bot.send_photo.return_value = reply
        else:
            bot.send_photo.side_effect = RuntimeError("wrong file identifier")
        return bot

    async def _deliver(self, bot: AsyncMock) -> None:
        settings = SimpleNamespace(admin_ids=[self.ADMIN_CHAT], currency="toman")
        async with self.Session() as session:
            user, order, payment = await self._seed(session)
            with patch.object(notifications, "get_settings", return_value=settings):
                await notify_auto_approve(bot, session, payment)
        self.user_id = user.id

    async def test_the_admin_gets_one_photo_message_with_caption_and_buttons(self) -> None:
        bot = self._bot()
        await self._deliver(bot)
        bot.send_photo.assert_awaited_once()
        kwargs = bot.send_photo.await_args.kwargs
        self.assertEqual((bot.send_photo.await_args.args[0], kwargs["photo"]), (self.ADMIN_CHAT, FILE_ID))
        self.assertIn("خودکار تأیید شد", kwargs["caption"])
        self.assertEqual(_callbacks(kwargs["reply_markup"]), [f"adm:users:view:{self.user_id}"])
        bot.send_message.assert_not_awaited()

    async def test_when_telegram_rejects_the_photo_the_text_notice_still_arrives_with_buttons(self) -> None:
        bot = self._bot(photo_works=False)
        await self._deliver(bot)
        bot.send_message.assert_awaited_once()
        self.assertEqual(_callbacks(bot.send_message.await_args.kwargs["reply_markup"]), [f"adm:users:view:{self.user_id}"])


if __name__ == "__main__":
    unittest.main()
