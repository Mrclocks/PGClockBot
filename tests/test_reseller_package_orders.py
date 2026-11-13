"""Package renew/upgrade paid like a plan: frozen order, any payment method, applied once."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from app.bot.handlers import reseller as reseller_handlers
from app.bot.handlers import reseller_package as bot
from app.db.models import (
    BotUser,
    Order,
    OrderStatus,
    Payment,
    PaymentMethod,
    PgAdminSubscription,
    ResellerProfile,
)
from app.services.approval_rule import is_delayed_purchase
from app.services.orders import fulfill_paid_order, is_mutation_order_note
from app.services.reseller_package import STALE_PAGE, renew_package
from app.services.reseller_package_orders import (
    INVALID_ORDER,
    NOT_FOR_SHOP_BOT,
    STALE_ORDER,
    apply_package_order,
    create_package_order,
    format_note,
    parse_note,
)
from tests.test_reseller_package_bot import ResellerPackageBotTests, buttons


class PackageOrderNoteTests(ResellerPackageBotTests):
    async def test_notes_round_trip_and_malformed_ones_are_ignored(self) -> None:
        self.assertEqual(parse_note(format_note(2, 5)), (2, 5))
        for bad in ("res_pkg:1", "res_pkg:1:2:3", "res_cap:gb:5", "res_pkg:x:1", "res_pkg:-1:0", "", None):
            self.assertIsNone(parse_note(bad), bad)
        self.assertTrue(is_mutation_order_note("res_pkg:1:5"))
        card = SimpleNamespace(is_wallet_topup=False, method=PaymentMethod.CARD.value)
        self.assertFalse(is_delayed_purchase(card, SimpleNamespace(note="res_pkg:1:5")))


class PackageOrderServiceTests(ResellerPackageBotTests):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.pg.reset_admin_usage = AsyncMock(return_value={})

    async def create(self, plan_id: int = 1, generation: int = 5) -> Order:
        owner = await self.session.get(BotUser, 3)
        profile = await self.session.get(ResellerProfile, 1)
        return await create_package_order(
            self.session, owner=owner, profile=profile, plan_id=plan_id, generation=generation,
        )

    async def pay(self, order_id: int) -> None:
        self.session.expire_all()
        row = await self.session.get(Order, order_id)
        row.status, row.payment_method = OrderStatus.PAID.value, PaymentMethod.CARD.value
        self.session.add(
            Payment(
                order_id=order_id,
                user_id=row.user_id,
                amount=row.amount,
                method=PaymentMethod.CARD.value,
                status="approved",
            )
        )
        await self.session.commit()

    async def sub(self) -> PgAdminSubscription:
        self.session.expire_all()
        return (
            await self.session.execute(
                select(PgAdminSubscription).where(PgAdminSubscription.pg_username == "shop3")
            )
        ).scalar_one()

    async def order_row(self, order_id: int) -> Order:
        self.session.expire_all()
        return await self.session.get(Order, order_id)

    async def test_order_freezes_plan_generation_price_and_belongs_to_the_platform(self) -> None:
        order = await self.create(1, 5)
        self.assertIsNone(order.reseller_id)
        self.assertEqual(order.user_id, 3)
        self.assertEqual(order.note, "res_pkg:1:5")
        self.assertEqual(order.status, "pending")
        self.assertEqual(order.amount, 100_000)
        self.assertEqual(await self.wallet(), 1_000_000, "creating an order charges nothing")

    async def test_upgrade_order_freezes_the_target_plan(self) -> None:
        order = await self.create(2, 5)
        self.assertEqual(order.note, "res_pkg:2:5")
        self.assertEqual(order.amount, 200_000)

    async def test_stale_generation_and_blocked_targets_are_refused(self) -> None:
        with self.assertRaises(ValueError) as stale:
            await self.create(1, 4)
        self.assertEqual(str(stale.exception), STALE_PAGE)
        with self.assertRaises(ValueError):
            await self.create(4, 5)  # other panel role
        with self.assertRaises(ValueError):
            await self.create(3, 5)  # too small for usage
        self.assertEqual(len(list(await self.session.scalars(select(Order)))), 0)

    async def test_paid_order_renews_without_touching_the_wallet(self) -> None:
        order = await self.create(1, 5)
        order_id = int(order.id)
        await self.pay(order_id)
        done = await fulfill_paid_order(self.session, await self.order_row(order_id))
        self.assertEqual(done.status, "delivered")
        self.assertEqual(await self.wallet(), 1_000_000)
        self.assertEqual(int((await self.sub()).renew_generation or 0), 6)
        self.assertFalse((await self.order_row(order_id)).service_mutation_pending)

    async def test_upgrade_via_order_moves_the_profile_plan(self) -> None:
        order = await self.create(2, 5)
        order_id = int(order.id)
        await self.pay(order_id)
        await fulfill_paid_order(self.session, await self.order_row(order_id))
        self.session.expire_all()
        self.assertEqual((await self.session.get(ResellerProfile, 1)).plan_id, 2)
        self.assertEqual(await self.wallet(), 1_000_000)

    async def test_a_second_fulfill_does_not_renew_again(self) -> None:
        order = await self.create(1, 5)
        order_id = int(order.id)
        await self.pay(order_id)
        await fulfill_paid_order(self.session, await self.order_row(order_id))
        again = await apply_package_order(self.session, await self.order_row(order_id))
        self.assertEqual(again.status, "delivered")
        self.assertEqual(int((await self.sub()).renew_generation or 0), 6)

    async def test_stale_order_after_wallet_renew_stays_paid(self) -> None:
        order = await self.create(1, 5)
        order_id = int(order.id)
        owner = await self.session.get(BotUser, 3)
        profile = await self.session.get(ResellerProfile, 1)
        await renew_package(self.session, owner=owner, profile=profile, plan_id=1, generation=5)
        await self.pay(order_id)
        with self.assertRaises(ValueError) as caught:
            await fulfill_paid_order(self.session, await self.order_row(order_id))
        self.assertEqual(str(caught.exception), STALE_ORDER)
        self.assertEqual((await self.order_row(order_id)).status, "paid")

    async def test_shop_scoped_orders_are_never_applied(self) -> None:
        order = await self.create(1, 5)
        order_id = int(order.id)
        row = await self.order_row(order_id)
        row.status, row.reseller_id = "paid", 5
        await self.session.commit()
        with self.assertRaises(ValueError) as caught:
            await apply_package_order(self.session, await self.order_row(order_id))
        self.assertEqual(str(caught.exception), INVALID_ORDER)


class PackageOrderBotTests(ResellerPackageBotTests):
    async def press_order(self, data: str, *, is_reseller_bot: bool = False):
        callback = MagicMock(data=data, answer=AsyncMock(), message=MagicMock())
        present = AsyncMock()
        with patch("app.bot.menu_nav.present_order_pay", present):
            await bot.res_package_order(
                callback,
                self.session,
                await self.session.get(BotUser, 3),
                MagicMock(),
                is_reseller_bot=is_reseller_bot,
            )
        return callback, present

    async def test_renew_screen_offers_other_methods_on_the_platform_bot(self) -> None:
        await self.tap(reseller_handlers.res_renew, "res:renew")
        shown = buttons(self.edit.await_args.kwargs["reply_markup"])
        self.assertIn("res:pkg:ok:1:5", shown)
        self.assertIn("res:pkg:o:1:5", shown)
        self.assertIn("کیف پول", shown["res:pkg:ok:1:5"])

    async def test_confirm_screen_offers_other_methods(self) -> None:
        await self.tap(bot.res_package_view, "res:pkg:v:2:5")
        shown = buttons(self.edit.await_args.kwargs["reply_markup"])
        self.assertIn("res:pkg:ok:2:5", shown)
        self.assertIn("res:pkg:o:2:5", shown)

    async def test_platform_bot_creates_an_order_and_shows_payment_methods(self) -> None:
        _callback, present = await self.press_order("res:pkg:o:1:5")
        present.assert_awaited_once()
        self.assertEqual(present.await_args.kwargs["is_reseller_bot"], False)
        orders = list(await self.session.scalars(select(Order)))
        self.assertEqual([(o.note, o.reseller_id) for o in orders], [("res_pkg:1:5", None)])

    async def test_shop_bot_never_offers_the_shops_own_payment_methods(self) -> None:
        callback, present = await self.press_order("res:pkg:o:1:5", is_reseller_bot=True)
        present.assert_not_awaited()
        self.assertEqual(callback.answer.await_args.args[0], NOT_FOR_SHOP_BOT)
        self.assertIsNone(bot.order_callback_for(1, 5, is_reseller_bot=True))
        self.assertEqual(len(list(await self.session.scalars(select(Order)))), 0)

    async def test_forged_buttons_create_nothing(self) -> None:
        for data in ("res:pkg:o:1:x", "res:pkg:o:1", "res:pkg:o:evil:1", "res:pkg:o:0:5", "res:pkg:o:1:4"):
            _callback, present = await self.press_order(data)
            present.assert_not_awaited()
        self.assertEqual(len(list(await self.session.scalars(select(Order)))), 0)
