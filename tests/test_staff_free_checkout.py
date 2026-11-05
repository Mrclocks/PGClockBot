"""Staff free checkout: who may use it, a single zero-amount claim, and delivery failures."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import (
    Base,
    BotUser,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Plan,
    ResellerProfile,
    Role,
)
from app.services import staff_free_checkout as free

SHOP_ID = 2
OTHER_SHOP_ID = 3
SHOP_ADMIN_TG = 2009
PRICE = 120_000


class ShopFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'free.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.sessions()
        self.session.add_all(
            [
                BotUser(id=SHOP_ID, telegram_id=1002, referral_code="s2", role=Role.RESELLER.value),
                BotUser(id=OTHER_SHOP_ID, telegram_id=1003, referral_code="s3", role=Role.RESELLER.value),
                BotUser(id=9, telegram_id=SHOP_ADMIN_TG, referral_code="a9", role=Role.USER.value, reseller_id=SHOP_ID),
                BotUser(id=10, telegram_id=1010, referral_code="c10", role=Role.USER.value, reseller_id=SHOP_ID),
                BotUser(id=11, telegram_id=1011, referral_code="p11", role=Role.ADMIN.value),
            ]
        )
        await self.session.flush()
        self.session.add_all(
            [
                ResellerProfile(
                    user_id=SHOP_ID,
                    is_active=True,
                    web_permissions="dashboard,free_checkout",
                    bot_permissions="dashboard,free_checkout",
                    bot_admin_ids=str(SHOP_ADMIN_TG),
                ),
                ResellerProfile(
                    user_id=OTHER_SHOP_ID,
                    is_active=True,
                    web_permissions="dashboard",
                    bot_permissions="dashboard",
                ),
                Plan(id=1, name="Gold", price=PRICE, duration_days=30, data_limit_gb=30),
            ]
        )
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def order(self, *, user_id: int, shop_id: int | None, note: str | None = None) -> Order:
        order = Order(user_id=user_id, plan_id=1, reseller_id=shop_id, amount=PRICE, note=note)
        self.session.add(order)
        await self.session.commit()
        return order

    async def user(self, user_id: int) -> BotUser:
        return await self.session.get(BotUser, user_id)

    async def may(self, actor_id: int, order: Order, *, shop_bot: int | None) -> bool:
        return await free.may_take_order_free(
            self.session,
            await self.user(actor_id),
            order,
            is_reseller_bot=shop_bot is not None,
            reseller_owner_id=shop_bot,
        )


class StaffFreeCheckoutTests(ShopFixture):
    async def test_shop_owner_and_bot_admin_may_take_their_own_order(self) -> None:
        self.assertTrue(await self.may(SHOP_ID, await self.order(user_id=SHOP_ID, shop_id=SHOP_ID), shop_bot=SHOP_ID))
        self.assertTrue(await self.may(9, await self.order(user_id=9, shop_id=SHOP_ID), shop_bot=SHOP_ID))

    async def test_a_customer_cannot_take_an_order_free(self) -> None:
        self.assertFalse(await self.may(10, await self.order(user_id=10, shop_id=SHOP_ID), shop_bot=SHOP_ID))

    async def test_staff_cannot_take_someone_elses_order(self) -> None:
        self.assertFalse(await self.may(SHOP_ID, await self.order(user_id=10, shop_id=SHOP_ID), shop_bot=SHOP_ID))

    async def test_a_shop_without_the_permission_gets_no_free_checkout(self) -> None:
        order = await self.order(user_id=OTHER_SHOP_ID, shop_id=OTHER_SHOP_ID)
        self.assertFalse(await self.may(OTHER_SHOP_ID, order, shop_bot=OTHER_SHOP_ID))

    async def test_a_shop_operator_cannot_use_it_for_another_shops_order(self) -> None:
        order = await self.order(user_id=SHOP_ID, shop_id=OTHER_SHOP_ID)
        self.assertFalse(await self.may(SHOP_ID, order, shop_bot=SHOP_ID))

    async def test_renewals_and_shop_capacity_orders_are_excluded(self) -> None:
        for note in ("renew:5", "res_cap:1", "res_pkg:1", "reseller_app:1", "svc_addon:5"):
            order = await self.order(user_id=SHOP_ID, shop_id=SHOP_ID, note=note)
            self.assertFalse(await self.may(SHOP_ID, order, shop_bot=SHOP_ID), note)

    async def test_platform_owner_may_take_a_platform_order_only(self) -> None:
        owner = MagicMock()
        with (
            patch.object(free, "resolve_bot_org_principal", AsyncMock(return_value=owner)),
            patch.object(free, "is_owner_principal", return_value=True),
        ):
            self.assertTrue(await self.may(11, await self.order(user_id=11, shop_id=None), shop_bot=None))
            self.assertFalse(await self.may(11, await self.order(user_id=11, shop_id=SHOP_ID), shop_bot=None))

    async def test_a_non_owner_on_the_platform_bot_is_refused(self) -> None:
        with patch.object(free, "resolve_bot_org_principal", AsyncMock(return_value=None)):
            self.assertFalse(await self.may(10, await self.order(user_id=10, shop_id=None), shop_bot=None))

    async def test_claim_zeroes_the_amount_and_records_one_free_payment(self) -> None:
        order = await self.order(user_id=SHOP_ID, shop_id=SHOP_ID)

        claimed = await free.claim_staff_free_order(self.session, order, actor_id=SHOP_ID)

        self.assertEqual(claimed.status, OrderStatus.PAID.value)
        self.assertEqual(claimed.amount, 0)
        self.assertEqual(claimed.discount_amount, PRICE)
        self.assertEqual(claimed.payment_method, free.STAFF_FREE_METHOD)
        payments = (await self.session.scalars(select(Payment).where(Payment.order_id == order.id))).all()
        self.assertEqual([(p.amount, p.method, p.status) for p in payments],
                         [(0, free.STAFF_FREE_METHOD, PaymentStatus.APPROVED.value)])

    async def test_an_earlier_pending_payment_is_closed_by_the_claim(self) -> None:
        order = await self.order(user_id=SHOP_ID, shop_id=SHOP_ID)
        self.session.add(Payment(order_id=order.id, user_id=SHOP_ID, amount=PRICE, status=PaymentStatus.PENDING.value))
        await self.session.commit()

        await free.claim_staff_free_order(self.session, order, actor_id=SHOP_ID)

        statuses = (
            await self.session.scalars(
                select(Payment.status).where(Payment.order_id == order.id).order_by(Payment.id)
            )
        ).all()
        self.assertEqual(statuses, [PaymentStatus.REJECTED.value, PaymentStatus.APPROVED.value])

    async def test_two_claims_at_once_record_a_single_payment(self) -> None:
        order_id = (await self.order(user_id=SHOP_ID, shop_id=SHOP_ID)).id

        async def claim() -> str:
            async with self.sessions() as session:
                try:
                    await free.claim_staff_free_order(session, await session.get(Order, order_id), actor_id=SHOP_ID)
                except ValueError:
                    return "refused"
                return "claimed"

        results = await asyncio.gather(claim(), claim())

        self.assertEqual(sorted(results), ["claimed", "refused"])
        count = await self.session.scalar(select(func.count()).select_from(Payment).where(Payment.order_id == order_id))
        self.assertEqual(count, 1)

    async def test_a_definite_delivery_failure_cancels_the_order(self) -> None:
        order = await self.order(user_id=SHOP_ID, shop_id=SHOP_ID)
        with patch.object(free, "deliver_order", AsyncMock(side_effect=ValueError("no group"))):
            delivered, stuck, error = await free.deliver_staff_free_order(self.session, order, actor_id=SHOP_ID)

        self.assertIsNone(delivered)
        self.assertIsNone(stuck)
        self.assertIsInstance(error, ValueError)
        await self.session.refresh(order)
        self.assertEqual(order.status, OrderStatus.CANCELLED.value)
        payment = await self.session.scalar(select(Payment).where(Payment.order_id == order.id))
        self.assertEqual(payment.status, PaymentStatus.REJECTED.value)

    async def test_an_uncertain_delivery_keeps_the_order_paid_for_staff(self) -> None:
        order = await self.order(user_id=SHOP_ID, shop_id=SHOP_ID)

        async def lost_answer(_session, _order, *, attempt):
            attempt.panel_may_hold_account = True
            raise TimeoutError("panel did not answer")

        with (
            patch.object(free, "deliver_order", lost_answer),
            patch("app.services.ux20.note_delivery_send_failure", AsyncMock()),
        ):
            delivered, stuck, _error = await free.deliver_staff_free_order(self.session, order, actor_id=SHOP_ID)

        self.assertIsNone(delivered)
        self.assertIsNotNone(stuck)
        await self.session.refresh(order)
        self.assertEqual(order.status, OrderStatus.PAID.value)

    async def test_a_delivered_order_cannot_be_claimed_again(self) -> None:
        order = await self.order(user_id=SHOP_ID, shop_id=SHOP_ID)
        order.status = OrderStatus.DELIVERED.value
        await self.session.commit()

        with self.assertRaises(ValueError):
            await free.claim_staff_free_order(self.session, order, actor_id=SHOP_ID)


class StaffFreeButtonTests(unittest.TestCase):
    def test_the_button_is_offered_only_when_allowed(self) -> None:
        from app.bot import keyboards as kb

        def callbacks(markup) -> list[str]:
            return [button.callback_data for row in markup.inline_keyboard for button in row]

        self.assertIn("pay:staff:7", callbacks(kb.pay_methods(7, {}, staff_free=True)))
        self.assertNotIn("pay:staff:7", callbacks(kb.pay_methods(7, {})))


class StaffFreeHandlerTests(ShopFixture):
    async def tap(self, actor_id: int, order: Order) -> MagicMock:
        from app.bot.handlers import staff_free_checkout as handler

        callback = MagicMock(data=f"pay:staff:{order.id}", answer=AsyncMock(), message=MagicMock())
        with (
            patch.object(handler, "is_customer_order_in_current_shop", return_value=True),
            patch.object(handler, "deliver_staff_free_order", AsyncMock()) as deliver,
            patch.object(handler, "safe_edit_text", AsyncMock()),
        ):
            await handler.pay_staff_free_cb(
                callback, self.session, await self.user(actor_id), is_reseller_bot=True, reseller_owner_id=SHOP_ID
            )
        callback.deliver = deliver
        return callback

    async def test_a_customer_tapping_a_forged_button_gets_nothing(self) -> None:
        from app.services import bot_feedback_texts as feedback

        callback = await self.tap(10, await self.order(user_id=10, shop_id=SHOP_ID))

        callback.deliver.assert_not_awaited()
        self.assertEqual(callback.answer.await_args.args[0], feedback.NO_ACCESS)

    async def test_a_delivered_free_order_sends_the_service_card_without_an_interim_notice(self) -> None:
        from app.bot.handlers import staff_free_checkout as handler

        order = await self.order(user_id=SHOP_ID, shop_id=SHOP_ID)
        callback = MagicMock(data=f"pay:staff:{order.id}", answer=AsyncMock(), message=MagicMock(delete=AsyncMock()))
        edit = AsyncMock()
        with (
            patch.object(handler, "is_customer_order_in_current_shop", return_value=True),
            patch.object(handler, "deliver_staff_free_order", AsyncMock(return_value=(order, None, None))),
            patch.object(handler, "send_delivery_to_user", AsyncMock()) as card,
            patch("app.services.notifications.notify_new_subscription", AsyncMock()),
            patch.object(handler, "safe_edit_text", edit),
        ):
            await handler.pay_staff_free_cb(
                callback, self.session, await self.user(SHOP_ID), is_reseller_bot=True, reseller_owner_id=SHOP_ID
            )

        callback.message.delete.assert_awaited_once()
        card.assert_awaited_once()
        edit.assert_not_awaited()
