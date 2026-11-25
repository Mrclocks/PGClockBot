"""Unfinished purchases with a random discount: abandoned ones are released, rejected receipts do not count, and a
customer cancels their own unpaid order."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import func, select

from app.bot.handlers import checkout_cancel as handler
from app.bot.keyboards import pay_methods
from app.db.models import Order, OrderStatus, Payment, RandomDiscountDraw
from app.services.checkout_cancel import NOT_CANCELLABLE, NOT_FOUND, cancel_customer_checkout
from app.services.orders import create_order
from app.services.random_discount_abandoned import open_draws_message, wait_minutes
from app.services.random_discounts import MAX_OPEN_DRAWS_PER_USER
from tests.test_random_discounts import RandomDiscountCase


class AbandonedCheckoutTests(RandomDiscountCase):
    async def orders_for_new_plans(self, count: int) -> list[Order]:
        plan_ids = await self._plans_with_distinct_prices(count + 1)
        self.extra_plan = plan_ids[-1]
        return [await create_order(self.session, user_id=1, plan_id=plan_id) for plan_id in plan_ids[:-1]]

    async def status(self, order_id: int) -> str:
        return await self.session.scalar(
            select(Order.status).where(Order.id == order_id).execution_options(populate_existing=True)
        )

    async def test_a_new_purchase_releases_the_oldest_abandoned_one_at_the_limit(self) -> None:
        await self.policy(minimum=10, maximum=500)
        orders = await self.orders_for_new_plans(MAX_OPEN_DRAWS_PER_USER)
        extra = await create_order(self.session, user_id=1, plan_id=self.extra_plan)
        self.assertIsNotNone(extra.random_discount_id)
        self.assertEqual(await self.status(orders[0].id), OrderStatus.CANCELLED.value)
        self.assertEqual(await self.status(orders[1].id), OrderStatus.PENDING.value)

    async def test_an_order_with_a_payment_is_never_released_automatically(self) -> None:
        await self.policy(minimum=10, maximum=500)
        orders = await self.orders_for_new_plans(MAX_OPEN_DRAWS_PER_USER)
        self.session.add(Payment(order_id=orders[0].id, user_id=1, amount=orders[0].amount))
        await self.session.commit()
        await create_order(self.session, user_id=1, plan_id=self.extra_plan)
        self.assertEqual(await self.status(orders[0].id), OrderStatus.PENDING.value)
        self.assertEqual(await self.status(orders[1].id), OrderStatus.CANCELLED.value)

    async def test_another_customers_orders_are_never_released(self) -> None:
        await self.policy(minimum=10, maximum=500)
        plan_ids = await self._plans_with_distinct_prices(MAX_OPEN_DRAWS_PER_USER + 1)
        theirs = await create_order(self.session, user_id=4, plan_id=plan_ids[0])
        for plan_id in plan_ids[1:]:
            await create_order(self.session, user_id=1, plan_id=plan_id)
        self.assertEqual(await self.status(theirs.id), OrderStatus.PENDING.value)

    async def test_a_rejected_receipt_does_not_count_toward_the_limit(self) -> None:
        await self.policy(minimum=10, maximum=500)
        orders = await self.orders_for_new_plans(MAX_OPEN_DRAWS_PER_USER)
        for order in orders:
            self.session.add(Payment(order_id=order.id, user_id=1, amount=order.amount, status="rejected",
                                     receipt_file_id="photo"))
            order.status = OrderStatus.REJECTED.value
        await self.session.commit()
        extra = await create_order(self.session, user_id=1, plan_id=self.extra_plan)
        self.assertIsNotNone(extra.random_discount_id)
        self.assertEqual(await self.status(orders[0].id), OrderStatus.REJECTED.value)

    async def test_a_new_purchase_of_the_same_plan_takes_over_the_abandoned_order(self) -> None:
        await self.policy()
        first = await create_order(self.session, user_id=1, plan_id=1)
        second = await create_order(self.session, user_id=1, plan_id=1)
        self.assertEqual(second.amount, first.amount)
        self.assertEqual(await self.status(first.id), OrderStatus.CANCELLED.value)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(RandomDiscountDraw)), 1)


class CustomerCancelTests(RandomDiscountCase):
    async def test_a_customer_cancels_their_own_unpaid_order(self) -> None:
        order = await create_order(self.session, user_id=1, plan_id=1)
        cancelled = await cancel_customer_checkout(self.session, user_id=1, order_id=order.id, shop_id=None)
        self.assertEqual(cancelled.status, OrderStatus.CANCELLED.value)

    async def test_another_customer_or_shop_cannot_cancel_it(self) -> None:
        order = await create_order(self.session, user_id=1, plan_id=1)
        order_id = int(order.id)
        for kwargs in ({"user_id": 4, "shop_id": None}, {"user_id": 1, "shop_id": 2}):
            with self.subTest(**kwargs), self.assertRaises(ValueError) as refused:
                await cancel_customer_checkout(self.session, order_id=order_id, **kwargs)
            self.assertEqual(str(refused.exception), NOT_FOUND)
        self.assertEqual(await self.session.scalar(select(Order.status).where(Order.id == order_id)), OrderStatus.PENDING.value)

    async def test_an_order_with_a_receipt_or_an_open_online_payment_cannot_be_cancelled(self) -> None:
        for payment in ({"receipt_file_id": "photo"}, {"method": "gateway"}, {"status": "approved"}):
            with self.subTest(payment=payment):
                order = await create_order(self.session, user_id=1, plan_id=1)
                order_id = int(order.id)
                self.session.add(Payment(order_id=order_id, user_id=1, amount=order.amount, **payment))
                await self.session.commit()
                with self.assertRaises(ValueError) as refused:
                    await cancel_customer_checkout(self.session, user_id=1, order_id=order_id, shop_id=None)
                self.assertEqual(str(refused.exception), NOT_CANCELLABLE)
                status = await self.session.scalar(select(Order.status).where(Order.id == order_id))
                self.assertNotEqual(status, OrderStatus.CANCELLED.value)

    async def test_a_second_cancel_is_refused_and_changes_nothing(self) -> None:
        order = await create_order(self.session, user_id=1, plan_id=1)
        await cancel_customer_checkout(self.session, user_id=1, order_id=order.id, shop_id=None)
        with self.assertRaises(ValueError):
            await cancel_customer_checkout(self.session, user_id=1, order_id=order.id, shop_id=None)


class MessageTests(RandomDiscountCase):
    def test_the_refusal_says_how_long_to_wait_and_where_to_cancel(self) -> None:
        from datetime import datetime, timedelta, timezone

        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(wait_minutes(now + timedelta(seconds=61), now), 2)
        self.assertEqual(wait_minutes(now - timedelta(minutes=5), now), 1)
        text = open_draws_message(now + timedelta(minutes=30), now)
        self.assertIn("حدود 30 دقیقه", text)
        self.assertIn("سفارش‌های من", text)
        self.assertNotIn("دقیقه", open_draws_message(None, now))


def _callback(data: str) -> MagicMock:
    callback = MagicMock(data=data)
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    return callback


class BotCancelTests(RandomDiscountCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.home = patch.object(handler, "render_home", AsyncMock())
        self.edit = patch.object(handler, "safe_edit_text", AsyncMock())
        self.render_home = self.home.start()
        self.safe_edit = self.edit.start()
        self.addCleanup(self.home.stop)
        self.addCleanup(self.edit.stop)
        self.customer = SimpleNamespace(id=1)
        self.state = AsyncMock()

    async def status(self, order_id: int) -> str:
        return await self.session.scalar(select(Order.status).where(Order.id == order_id))

    def test_the_payment_screens_cancel_button_cancels_this_order(self) -> None:
        rows = pay_methods(7, {"btn_cancel": "❌ انصراف"}).inline_keyboard
        self.assertEqual(rows[-1][-1].callback_data, "pay:x:7")

    async def test_cancel_on_the_payment_screen_cancels_and_goes_home(self) -> None:
        order_id = int((await create_order(self.session, user_id=1, plan_id=1)).id)
        callback = _callback(f"pay:x:{order_id}")
        await handler.cancel_from_payment(callback, self.session, self.customer, self.state)
        self.assertEqual(await self.status(order_id), OrderStatus.CANCELLED.value)
        callback.answer.assert_awaited_with(handler.CANCELLED, show_alert=False)
        self.render_home.assert_awaited_once()

    async def test_cancel_with_an_online_payment_open_only_goes_home(self) -> None:
        order = await create_order(self.session, user_id=1, plan_id=1)
        order_id = int(order.id)
        self.session.add(Payment(order_id=order_id, user_id=1, amount=order.amount, method="gateway"))
        await self.session.commit()
        callback = _callback(f"pay:x:{order_id}")
        await handler.cancel_from_payment(callback, self.session, self.customer, self.state)
        self.assertEqual(await self.status(order_id), OrderStatus.PENDING.value)
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        self.render_home.assert_awaited_once()

    async def test_another_customers_order_is_not_cancelled_from_a_card(self) -> None:
        order_id = int((await create_order(self.session, user_id=4, plan_id=1)).id)
        callback = _callback(f"myord:xy:{order_id}")
        await handler.confirm_cancel(callback, self.session, self.customer)
        self.assertEqual(await self.status(order_id), OrderStatus.PENDING.value)
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])

    async def test_the_order_card_asks_before_cancelling(self) -> None:
        order_id = int((await create_order(self.session, user_id=1, plan_id=1)).id)
        await handler.ask_to_cancel(_callback(f"myord:x:{order_id}"), self.session)
        self.assertEqual(await self.status(order_id), OrderStatus.PENDING.value)
        markup = self.safe_edit.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, f"myord:xy:{order_id}")
        await handler.confirm_cancel(_callback(f"myord:xy:{order_id}"), self.session, self.customer)
        self.assertEqual(await self.status(order_id), OrderStatus.CANCELLED.value)
