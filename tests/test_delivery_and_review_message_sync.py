"""Real async ORM regressions for delivery and multi-admin payment cards."""

from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.db import Base
from app.db.models import (
    BotUser, Order, OrderStatus, Payment, PaymentReviewMessage, PaymentStatus,
    Plan, Role, UserService,
)
from app.services.orders import approve_payment, reject_payment
from app.services.payment_review_messages import remember_review_message, sync_review_messages


class AsyncDatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            user = BotUser(telegram_id=100, referral_code="buyer")
            admin = BotUser(
                telegram_id=201, referral_code="admin1", role=Role.ADMIN.value,
                full_name="علی <ادمین>",
            )
            other = BotUser(
                telegram_id=202, referral_code="admin2", role=Role.ADMIN.value,
                full_name="رضا",
            )
            session.add_all([user, admin, other])
            await session.flush()
            payment = Payment(user_id=user.id, amount=1000, is_wallet_topup=True)
            session.add(payment)
            await session.commit()
            self.user_id, self.payment_id = user.id, payment.id
        self.bot = MagicMock(spec=Bot)
        self.bot.id = 500
        self.bot.token = "500:test"
        self.bot.send_message = AsyncMock(side_effect=self._send_message)
        self.bot.send_photo = AsyncMock(side_effect=self._send_photo)
        self.bot.edit_message_text = AsyncMock()
        self.bot.edit_message_caption = AsyncMock()
        self.bot.session = SimpleNamespace(close=AsyncMock())

    async def asyncTearDown(self):
        await self.engine.dispose()

    def _message(self, chat_id, text, *, photo=False):
        return SimpleNamespace(
            message_id=chat_id + 1000, chat=SimpleNamespace(id=chat_id),
            photo=["receipt"] if photo else [],
            html_text=None if photo else text, html_caption=text if photo else None,
        )

    async def _send_message(self, chat_id, text, **kwargs):
        return self._message(chat_id, text)

    async def _send_photo(self, chat_id, *, caption, **kwargs):
        return self._message(chat_id, caption, photo=True)

    async def _notify(self, *, photo=False):
        from app.services.notifications import notify_pending_approval

        async with self.Session() as session:
            payment = await session.get(Payment, self.payment_id)
            payment.receipt_file_id = "receipt" if photo else None
            await session.commit()
            settings = SimpleNamespace(admin_ids=[201, 202, 201], currency="Toman")
            with patch("app.services.notifications.get_settings", return_value=settings), patch(
                "app.services.notifications.notify_enabled", new=AsyncMock(return_value=True)
            ):
                await notify_pending_approval(self.bot, session, payment, 100)


class DeliveryPlanLoadingTests(AsyncDatabaseTest):
    async def _order(self):
        async with self.Session() as session:
            plan = Plan(name="یک ماهه", price=1000)
            session.add(plan)
            await session.flush()
            service = UserService(bot_user_id=self.user_id, plan_id=plan.id, pg_username="buyer")
            session.add(service)
            await session.flush()
            order = Order(
                user_id=self.user_id, plan_id=plan.id, service_id=service.id,
                amount=1000, status=OrderStatus.DELIVERED.value,
            )
            session.add(order)
            await session.commit()
            return order.id

    async def test_fresh_order_loads_plan_without_implicit_io(self):
        from app.services.delivery import build_delivery_content

        order_id = await self._order()
        async with self.Session() as session:
            order = await session.get(Order, order_id)
            self.assertNotIn("plan", vars(order))
            with patch("app.services.delivery.get_all_settings", new=AsyncMock(return_value={})):
                content = await build_delivery_content(session, None, order)
            self.assertIn("یک ماهه", content["text"])
            self.assertEqual(order.plan.name, "یک ماهه")

    async def test_loaded_plan_survives_commit_and_delivery(self):
        from app.services.delivery import build_delivery_content

        order_id = await self._order()
        async with self.Session() as session:
            order = (await session.execute(
                select(Order).where(Order.id == order_id).options(selectinload(Order.plan))
            )).scalar_one()
            await session.commit()
            with patch("app.services.delivery.get_all_settings", new=AsyncMock(return_value={})):
                content = await build_delivery_content(session, None, order)
            self.assertIn("یک ماهه", content["text"])


def _callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class MultiAdminReviewTests(AsyncDatabaseTest):
    async def test_approve_updates_both_cards_with_original_reviewer(self):
        await self._notify()
        async with self.Session() as first, self.Session() as second:
            payment = await first.get(Payment, self.payment_id)
            stale = await second.get(Payment, self.payment_id)
            await approve_payment(first, payment, 201, bot=self.bot)
            self.assertEqual(self.bot.edit_message_text.await_count, 2)
            for call in self.bot.edit_message_text.await_args_list:
                self.assertIn("تأیید شد توسط علی &lt;ادمین&gt;", call.kwargs["text"])
                self.assertNotIn("نیاز به تأیید", call.kwargs["text"])
                self.assertNotIn("از دکمه‌های زیر", call.kwargs["text"])
                self.assertEqual(_callbacks(call.kwargs["reply_markup"]), [f"adm:users:view:{self.user_id}"])
            self.assertEqual(
                {c.kwargs["chat_id"] for c in self.bot.edit_message_text.await_args_list},
                {201, 202},
            )
            self.bot.edit_message_text.reset_mock()
            with self.assertRaises(ValueError):
                await reject_payment(second, stale, 202, bot=self.bot)
            for call in self.bot.edit_message_text.await_args_list:
                self.assertIn("تأیید شد توسط علی &lt;ادمین&gt;", call.kwargs["text"])
                self.assertNotIn("رضا", call.kwargs["text"])
        async with self.Session() as session:
            user = await session.get(BotUser, self.user_id)
            payment = await session.get(Payment, self.payment_id)
            self.assertEqual(user.wallet_balance, 1000)
            self.assertEqual(payment.reviewed_by, 201)
            self.assertEqual(payment.status, PaymentStatus.APPROVED.value)

    async def test_reject_edits_both_photo_captions_after_reopening_session(self):
        await self._notify(photo=True)
        async with self.Session() as session:
            rows = (await session.execute(select(PaymentReviewMessage))).scalars().all()
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(r.is_photo for r in rows))
            payment = await session.get(Payment, self.payment_id)
            await reject_payment(session, payment, 202, bot=self.bot, note="رسید خوانا نبود <b>")
        self.assertEqual(self.bot.edit_message_caption.await_count, 2)
        self.bot.edit_message_text.assert_not_awaited()
        for call in self.bot.edit_message_caption.await_args_list:
            self.assertIn("رد شد توسط رضا", call.kwargs["caption"])
            self.assertIn("رسید خوانا نبود &lt;b&gt;", call.kwargs["caption"])
            self.assertEqual(_callbacks(call.kwargs["reply_markup"]), [f"adm:users:view:{self.user_id}"])

    async def test_photo_failure_tracks_actual_fallback_text_message(self):
        self.bot.send_photo.side_effect = RuntimeError("photo unavailable")
        with self.assertLogs("app.services.notifications", level="WARNING"):
            await self._notify(photo=True)
        async with self.Session() as session:
            rows = (await session.execute(select(PaymentReviewMessage))).scalars().all()
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(not r.is_photo for r in rows))
            payment = await session.get(Payment, self.payment_id)
            await approve_payment(session, payment, 201, bot=self.bot)
        self.assertEqual(self.bot.edit_message_text.await_count, 2)
        self.bot.edit_message_caption.assert_not_awaited()

    async def test_edit_failure_does_not_block_other_admin_or_payment(self):
        await self._notify()

        async def edit(**kwargs):
            if kwargs["chat_id"] == 201:
                raise RuntimeError("message deleted")

        self.bot.edit_message_text.side_effect = edit
        # Alembic's fileConfig (run by earlier migration tests) disables loggers that already exist.
        logging.getLogger("app.services.payment_review_messages").disabled = False
        async with self.Session() as session:
            payment = await session.get(Payment, self.payment_id)
            with self.assertLogs("app.services.payment_review_messages", level="WARNING"):
                await approve_payment(session, payment, 201, bot=self.bot)
            self.assertEqual(self.bot.edit_message_text.await_count, 2)
            self.assertEqual(payment.status, PaymentStatus.APPROVED.value)

    async def test_delivery_failure_still_closes_all_approval_cards(self):
        await self._notify()
        async with self.Session() as session:
            order = Order(user_id=self.user_id, amount=1000, status=OrderStatus.AWAITING_APPROVAL.value)
            session.add(order)
            await session.flush()
            payment = await session.get(Payment, self.payment_id)
            payment.order_id, payment.is_wallet_topup = order.id, False
            await session.commit()
            with patch("app.services.orders.fulfill_paid_order", new=AsyncMock(side_effect=RuntimeError("PG offline"))):
                with self.assertRaisesRegex(RuntimeError, "PG offline"):
                    await approve_payment(session, payment, 201, bot=self.bot)
            self.assertEqual(payment.status, PaymentStatus.APPROVED.value)
        self.assertEqual(self.bot.edit_message_text.await_count, 2)

    async def test_other_bot_message_is_never_edited(self):
        await self._notify()
        async with self.Session() as session:
            session.add(PaymentReviewMessage(
                bot_id=999, chat_id=201, message_id=1201, payment_id=self.payment_id,
                text="another bot", is_photo=False,
            ))
            await session.commit()
            payment = await session.get(Payment, self.payment_id)
            await approve_payment(session, payment, 201, bot=self.bot)
        self.assertEqual(self.bot.edit_message_text.await_count, 2)
        self.assertTrue(all("another bot" not in c.kwargs["text"] for c in self.bot.edit_message_text.await_args_list))

    async def test_long_caption_stays_valid_and_mentions_reviewer(self):
        async with self.Session() as session:
            await remember_review_message(
                session, self.payment_id, self.bot,
                self._message(201, "<b>" + "x" * 1010 + "</b>", photo=True),
            )
            await session.commit()
            payment = await session.get(Payment, self.payment_id)
            await reject_payment(session, payment, 202, bot=self.bot)
        caption = self.bot.edit_message_caption.await_args.kwargs["caption"]
        self.assertLessEqual(len(caption), 1024)
        self.assertIn("رد شد توسط رضا", caption)

    async def test_approval_during_fanout_updates_late_recipient(self):
        from app.services.notifications import _dispatch_dual_notify

        async with self.Session() as session:
            payment = await session.get(Payment, self.payment_id)

            async def send(bot, targets, text, *, sent_messages, **kwargs):
                payment.status, payment.reviewed_by = PaymentStatus.APPROVED.value, 201
                await session.commit()
                sent_messages.extend(self._message(chat, text) for chat in targets)
                return len(targets)

            settings = SimpleNamespace(admin_ids=[201, 202])
            with patch("app.services.notifications.get_settings", return_value=settings), patch(
                "app.services.notifications.notify_enabled", new=AsyncMock(return_value=True)
            ), patch("app.services.notifications._send_to_chats", side_effect=send):
                await _dispatch_dual_notify(
                    self.bot, session, "notify_pending_approval", "⏳ نیاز به تأیید",
                    payment=payment, shop=False,
                )
        self.assertEqual(self.bot.edit_message_text.await_count, 2)

    async def test_shop_cards_use_shop_bot_even_when_reviewed_from_panel(self):
        async with self.Session() as session:
            owner = (await session.execute(select(BotUser).where(BotUser.telegram_id == 201))).scalar_one()
            order = Order(
                user_id=self.user_id, reseller_id=owner.id, amount=1000,
                status=OrderStatus.AWAITING_APPROVAL.value,
            )
            session.add(order)
            await session.flush()
            payment = await session.get(Payment, self.payment_id)
            payment.order_id, payment.is_wallet_topup = order.id, False
            await session.commit()
            rid = owner.id
        with patch(
            "app.services.notifications._shop_recipient_chat_ids", new=AsyncMock(return_value=[201, 202])
        ), patch(
            "app.services.resellers.get_reseller_profile",
            new=AsyncMock(return_value=SimpleNamespace(bot_token=self.bot.token)),
        ):
            await self._notify()
        async with self.Session() as session:
            payment = await session.get(Payment, self.payment_id)
            with patch(
                "app.services.reseller_bots.open_notify_bot_for_reseller",
                new=AsyncMock(return_value=(self.bot, True)),
            ) as open_shop, patch("app.services.payment_review_messages.Bot") as main_bot:
                await reject_payment(session, payment, 202)
            open_shop.assert_awaited_once_with(session, rid)
            main_bot.assert_not_called()
        self.assertEqual(self.bot.edit_message_text.await_count, 2)
        self.bot.session.close.assert_awaited_once()

    async def test_old_callback_is_remembered_and_approval_finish_does_not_overwrite(self):
        from app.bot.handlers.payments import _payrev_finish, _payrev_load

        message = self._message(201, "⏳ نیاز به تأیید")
        message.edit_text = AsyncMock()
        callback = SimpleNamespace(
            bot=self.bot, message=message, data=f"payrev:ok:{self.payment_id}",
            answer=AsyncMock(),
        )
        async with self.Session() as session:
            admin = (await session.execute(select(BotUser).where(BotUser.telegram_id == 201))).scalar_one()
            with patch("app.bot.handlers.payments.reseller_can_review_payment", new=AsyncMock(return_value=True)):
                payment = await _payrev_load(callback, session, admin)
                await _payrev_load(callback, session, admin)
            rows = (await session.execute(select(PaymentReviewMessage))).scalars().all()
            self.assertEqual(len(rows), 1)
            await approve_payment(session, payment, 201, bot=self.bot)
            await _payrev_finish(callback, SimpleNamespace(ok=True, alert_fa="تأیید شد", message_suffix="old suffix"))
        message.edit_text.assert_not_awaited()
        self.assertIn("تأیید شد توسط علی", self.bot.edit_message_text.await_args.kwargs["text"])


class ReviewMessageMigrationTests(unittest.TestCase):
    def test_existing_database_upgrade_and_downgrade(self):
        import importlib.util
        from pathlib import Path
        from alembic.operations import Operations
        from alembic.runtime.migration import MigrationContext
        from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect

        path = Path(__file__).resolve().parents[1] / "alembic/versions/0035_payment_review_messages.py"
        spec = importlib.util.spec_from_file_location("review_message_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as conn:
                Table("payments", MetaData(), Column("id", Integer, primary_key=True)).create(conn)
                with Operations.context(MigrationContext.configure(conn)):
                    migration.upgrade()
                    migration.upgrade()
                    inspector = inspect(conn)
                    self.assertTrue(inspector.has_table("payment_review_messages"))
                    self.assertEqual(
                        set(inspector.get_pk_constraint("payment_review_messages")["constrained_columns"]),
                        {"bot_id", "chat_id", "message_id"},
                    )
                    self.assertEqual(
                        inspector.get_foreign_keys("payment_review_messages")[0]["options"]["ondelete"],
                        "CASCADE",
                    )
                    migration.downgrade()
                    self.assertFalse(inspect(conn).has_table("payment_review_messages"))
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
