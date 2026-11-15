"""Capacity purchases paid like a plan: frozen order, any payment method, applied once, safe failures."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from sqlalchemy import select

from app.bot.handlers import reseller_capacity_buy as bot
from app.db.models import Order, OrderStatus, Payment, PaymentMethod, PgAdminSubscription, ResellerPlan
from app.services.approval_rule import is_delayed_purchase
from app.services.billing import GB
from app.services.orders import fulfill_paid_order, is_mutation_order_note, pay_with_wallet
from app.services.pasarguard import PasarGuardError
from app.services.reseller_capacity_orders import (
    INVALID_ORDER,
    NOT_APPLIED,
    NOT_FOR_SHOP_BOT,
    CapacityPendingReview,
    apply_capacity_order,
    create_capacity_order,
    format_note,
    parse_note,
)
from app.services.service_renewals import RenewalPendingReview
from tests.test_reseller_packs import PackTestBase


class CapacityOrderBase(PackTestBase):
    async def order(self, kind: str = "pack", ref: int = 11, *, user_id: int = 3, profile_id: int = 1) -> Order:
        from app.db.models import BotUser, ResellerProfile

        async with self.Session() as session:
            user = await session.get(BotUser, user_id)
            profile = await session.get(ResellerProfile, profile_id)
            plan = await session.get(ResellerPlan, int(profile.plan_id))
            return await create_capacity_order(session, user=user, profile=profile, plan=plan, kind=kind, ref=ref)

    async def pay(self, order_id: int, *, status: str = OrderStatus.PAID.value, method: str = PaymentMethod.CARD.value) -> None:
        """What approving a receipt does to the order before delivery."""
        async with self.Session() as session:
            row = await session.get(Order, order_id)
            row.status, row.payment_method = status, method
            session.add(Payment(order_id=order_id, user_id=row.user_id, amount=row.amount, method=method, status="approved"))
            await session.commit()

    async def fulfill(self, order_id: int) -> Order:
        async with self.Session() as session:
            return await fulfill_paid_order(session, await session.get(Order, order_id))

    async def row(self, order_id: int) -> Order:
        async with self.Session() as session:
            return await session.get(Order, order_id)

    async def extra_gb(self) -> int:
        async with self.Session() as session:
            sub = await session.scalar(select(PgAdminSubscription).where(PgAdminSubscription.pg_username == "shop3"))
            return int(sub.extra_gb_purchased or 0)


class NoteTests(CapacityOrderBase):
    async def test_notes_round_trip_and_malformed_ones_are_ignored(self) -> None:
        parsed = parse_note(format_note("pack", 11, gb=200, users=0))
        self.assertEqual((parsed.kind, parsed.ref, parsed.gb, parsed.users), ("pack", 11, 200, 0))
        self.assertEqual(parse_note("res_cap:gb:5").gb, 5)
        self.assertEqual(parse_note("res_cap:users:10").users, 10)
        for bad in ("res_cap:pack:1", "res_cap:gb:5:1:1", "res_cap:x:1", "res_cap:gb:-5", "res_cap:gb:5; DROP", "", None):
            self.assertIsNone(parse_note(bad), bad)

    async def test_capacity_orders_follow_the_mutation_rules(self) -> None:
        self.assertTrue(is_mutation_order_note("res_cap:gb:5"))
        card = SimpleNamespace(is_wallet_topup=False, method=PaymentMethod.CARD.value)
        self.assertFalse(is_delayed_purchase(card, SimpleNamespace(note="res_cap:gb:5")), "never auto-approved by the wait rule")


class CreateOrderTests(CapacityOrderBase):
    async def test_pack_order_freezes_entitlement_and_price_and_belongs_to_the_platform(self) -> None:
        order = await self.order("pack", 11)
        self.assertEqual((order.amount, order.status, order.reseller_id, order.user_id), (200_000, "pending", None, 3))
        self.assertEqual(order.note, "res_cap:pack:11:200:0")
        self.assertEqual((await self.state())[0], 1_000_000, "creating an order charges nothing")

    async def test_unit_order_uses_the_plan_rate(self) -> None:
        async with self.Session() as session:
            plan = await session.get(ResellerPlan, 1)
            plan.allow_buy_extra, plan.extra_gb_price = True, 1_000
            await session.commit()
        order = await self.order("gb", 10)
        self.assertEqual((order.amount, order.note), (10_000, "res_cap:gb:10"))

    async def test_orders_that_should_not_exist_are_refused(self) -> None:
        for kind, ref in (("pack", 12), ("pack", 14), ("pack", 999), ("gb", 7), ("gb", 5), ("users", 5), ("x", 1)):
            with self.subTest(kind=kind, ref=ref), self.assertRaises(ValueError):
                await self.order(kind, ref)
        async with self.Session() as session:
            self.assertEqual(len(list(await session.scalars(select(Order)))), 0)

    async def test_another_shops_profile_cannot_order_for_this_user(self) -> None:
        with self.assertRaises(ValueError):
            await self.order("pack", 10, user_id=3, profile_id=2)


class ApplyOrderTests(CapacityOrderBase):
    async def test_paid_order_raises_the_live_limit_once_without_touching_the_wallet(self) -> None:
        order = await self.order("pack", 11)
        await self.pay(order.id)
        done = await self.fulfill(order.id)
        self.assertEqual(done.status, "delivered")
        self.pg.modify_admin.assert_awaited_once_with("shop3", {"data_limit": 300 * GB})
        self.assertEqual((await self.state())[:2], (1_000_000, 200))
        self.assertFalse((await self.row(order.id)).service_mutation_pending)

    async def test_a_second_approval_does_not_apply_it_again(self) -> None:
        order = await self.order("pack", 11)
        await self.pay(order.id)
        await self.fulfill(order.id)
        async with self.Session() as session:
            again = await apply_capacity_order(session, await session.get(Order, order.id))
        self.assertEqual(again.status, "delivered")
        self.assertEqual((self.pg.modify_admin.await_count, await self.extra_gb()), (1, 200))

    async def test_price_change_after_the_order_does_not_change_what_was_bought(self) -> None:
        order = await self.order("pack", 11)
        async with self.Session() as session:
            pack = await session.get(ResellerPlan, 11)
            pack.price, pack.addon_gb = 999_999, 1
            await session.commit()
        await self.pay(order.id)
        await self.fulfill(order.id)
        self.assertEqual((await self.extra_gb(), (await self.row(order.id)).amount), (200, 200_000))

    async def test_panel_refusal_returns_the_order_to_paid_and_forgets_the_extra(self) -> None:
        order = await self.order("pack", 11)
        await self.pay(order.id)
        self.pg.modify_admin = AsyncMock(side_effect=PasarGuardError("bad", 422))
        with self.assertRaises(ValueError) as caught:
            await self.fulfill(order.id)
        self.assertEqual(str(caught.exception), NOT_APPLIED)
        row = await self.row(order.id)
        self.assertEqual((row.status, row.service_mutation_pending, await self.extra_gb()), ("paid", False, 0))
        self.pg.modify_admin = AsyncMock(return_value={})
        self.assertEqual((await self.fulfill(order.id)).status, "delivered")
        self.assertEqual(await self.extra_gb(), 200, "staff can retry the paid order")

    async def test_lost_answer_keeps_the_payment_and_flags_the_order_for_review(self) -> None:
        order = await self.order("pack", 11)
        await self.pay(order.id)
        self.pg.modify_admin = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
        with self.assertRaises(CapacityPendingReview) as caught:
            await self.fulfill(order.id)
        self.assertIsInstance(caught.exception, RenewalPendingReview)
        row = await self.row(order.id)
        self.assertEqual((row.status, row.service_mutation_pending, await self.extra_gb()), ("delivering", True, 200))
        self.pg.modify_admin = AsyncMock(return_value={})
        with self.assertRaises(ValueError):
            await self.fulfill(order.id)
        self.pg.modify_admin.assert_not_awaited()

    async def test_expired_subscription_at_delivery_applies_nothing(self) -> None:
        from datetime import datetime, timedelta, timezone

        order = await self.order("pack", 11)
        await self.pay(order.id)
        async with self.Session() as session:
            sub = await session.scalar(select(PgAdminSubscription).where(PgAdminSubscription.pg_username == "shop3"))
            sub.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
            await session.commit()
        with self.assertRaises(ValueError):
            await self.fulfill(order.id)
        self.assertEqual(((await self.row(order.id)).status, await self.extra_gb()), ("paid", 0))
        self.pg.modify_admin.assert_not_awaited()

    async def test_unpaid_and_shop_orders_are_never_applied(self) -> None:
        order = await self.order("pack", 11)
        with self.assertRaises(ValueError):
            async with self.Session() as session:
                await apply_capacity_order(session, await session.get(Order, order.id))
        async with self.Session() as session:
            row = await session.get(Order, order.id)
            row.status, row.reseller_id = "paid", 5
            await session.commit()
            with self.assertRaises(ValueError) as caught:
                await apply_capacity_order(session, row)
        self.assertEqual(str(caught.exception), INVALID_ORDER)
        self.assertEqual(await self.extra_gb(), 0)


class WalletOrderTests(CapacityOrderBase):
    async def test_wallet_payment_of_the_order_charges_once_and_applies(self) -> None:
        order = await self.order("pack", 11)
        async with self.Session() as session:
            from app.db.models import BotUser

            done = await pay_with_wallet(session, await session.get(Order, order.id), await session.get(BotUser, 3))
        self.assertEqual(done.status, "delivered")
        self.assertEqual((await self.state())[:2], (800_000, 200))

    async def test_wallet_payment_is_returned_when_the_panel_refuses(self) -> None:
        order = await self.order("pack", 11)
        self.pg.modify_admin = AsyncMock(side_effect=PasarGuardError("bad", 422))
        async with self.Session() as session:
            from app.db.models import BotUser

            with self.assertRaises(ValueError):
                await pay_with_wallet(session, await session.get(Order, order.id), await session.get(BotUser, 3))
        self.assertEqual((await self.state())[:2], (1_000_000, 0))
        self.assertEqual((await self.row(order.id)).status, "pending")

    async def test_wallet_payment_is_kept_when_the_answer_is_lost(self) -> None:
        order = await self.order("pack", 11)
        self.pg.modify_admin = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
        async with self.Session() as session:
            from app.db.models import BotUser

            with self.assertRaises(CapacityPendingReview):
                await pay_with_wallet(session, await session.get(Order, order.id), await session.get(BotUser, 3))
        self.assertEqual((await self.state())[:2], (800_000, 200), "no blind refund")


class BotOrderTests(CapacityOrderBase):
    async def press(self, data: str, *, is_reseller_bot: bool = False):
        callback = MagicMock(data=data, answer=AsyncMock(), message=MagicMock())
        present = AsyncMock()
        async with self.Session() as session:
            from app.db.models import BotUser

            with patch("app.bot.menu_nav.present_order_pay", present):
                await bot.res_capacity_order(
                    callback, session, await session.get(BotUser, 3), MagicMock(), is_reseller_bot=is_reseller_bot,
                )
        return callback, present

    async def test_platform_bot_creates_an_order_and_shows_the_payment_methods(self) -> None:
        callback, present = await self.press("res:cap:o:pack:11")
        present.assert_awaited_once()
        self.assertEqual(present.await_args.kwargs["is_reseller_bot"], False)
        async with self.Session() as session:
            orders = list(await session.scalars(select(Order)))
        self.assertEqual([(o.note, o.amount, o.reseller_id) for o in orders], [("res_cap:pack:11:200:0", 200_000, None)])

    async def test_a_shop_bot_never_offers_the_shops_own_payment_methods(self) -> None:
        callback, present = await self.press("res:cap:o:pack:11", is_reseller_bot=True)
        present.assert_not_awaited()
        self.assertEqual(callback.answer.await_args.args[0], NOT_FOR_SHOP_BOT)
        self.assertIsNone(bot.order_callback_for("pack", 11, is_reseller_bot=True))
        async with self.Session() as session:
            self.assertEqual(len(list(await session.scalars(select(Order)))), 0)

    async def test_forged_buttons_create_nothing(self) -> None:
        for data in ("res:cap:o:pack:x", "res:cap:o:pack", "res:cap:o:evil:1", "res:cap:o:pack:0", "res:cap:o:pack:1:2", "res:cap:o:pack:12"):
            _callback, present = await self.press(data)
            present.assert_not_awaited()
        async with self.Session() as session:
            self.assertEqual(len(list(await session.scalars(select(Order)))), 0)
