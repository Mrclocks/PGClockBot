"""Paid orders without a service: refund them to the wallet once, or record a manual delivery."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import (
    BotUser,
    DeliveryFailure,
    Order,
    Payment,
    ResellerApplication,
    ResellerPlan,
    ShopWallet,
    WalletTransaction,
)
from app.services.shop_scope import ShopScopeError
from app.services.stuck_order_resolution import (
    APPLICATION_APPROVED,
    MUTATION_MAY_BE_APPLIED,
    NOT_STUCK,
    cancel_and_refund,
    close_without_refund,
    count_still_stuck,
    describe_stuck_orders,
    mark_delivered_manually,
)
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

OWNER = OWNER_SESSION
SHOP = {"role": "reseller", "bot_user_id": 5}
OLD = datetime.now(timezone.utc) - timedelta(hours=2)


class StuckCase(RoutesHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=101, referral_code="u1", role="user", wallet_balance=1000),
                BotUser(id=6, telegram_id=106, referral_code="u6", role="user", reseller_id=5),
            ])
            await session.flush()
            session.add_all([
                Order(id=10, user_id=1, amount=50000, status="paid", updated_at=OLD),
                Order(id=11, user_id=6, reseller_id=5, amount=30000, status="paid", updated_at=OLD),
                Order(id=12, user_id=1, amount=1, status="paid", updated_at=datetime.now(timezone.utc)),
                Order(id=13, user_id=1, amount=1, status="delivered", updated_at=OLD),
                Order(id=14, user_id=1, amount=1, status="paid", note="renew:5", updated_at=OLD),
                Order(id=15, user_id=1, amount=0, status="delivering", updated_at=OLD),
                Order(id=16, user_id=1, amount=7000, status="paid", note="renew:9",
                      service_mutation_pending=True, updated_at=OLD),
                Order(id=17, user_id=1, amount=8000, status="paid", note="reseller_app:1", updated_at=OLD),
                Order(id=18, user_id=1, amount=9000, status="paid", note="reseller_app:2", updated_at=OLD),
            ])
            session.add(ResellerPlan(id=1, name="rp"))
            await session.flush()
            session.add_all([
                ResellerApplication(id=1, user_id=1, plan_id=1, status="awaiting_approval"),
                ResellerApplication(id=2, user_id=1, plan_id=1, status="approved"),
            ])
            await session.flush()
            session.add_all([
                Payment(order_id=10, user_id=1, amount=50000, method="card", status="approved"),
                Payment(order_id=11, user_id=6, amount=30000, method="wallet", status="approved"),
                Payment(order_id=12, user_id=1, amount=1, method="card", status="approved"),
                Payment(order_id=14, user_id=1, amount=4000, method="card", status="approved"),
                Payment(order_id=16, user_id=1, amount=7000, method="card", status="approved"),
                Payment(order_id=17, user_id=1, amount=8000, method="card", status="approved"),
                Payment(order_id=18, user_id=1, amount=9000, method="card", status="approved"),
                Payment(order_id=10, user_id=1, amount=999, method="card", status="rejected"),
                DeliveryFailure(order_id=10, error="x"),
            ])
            await session.commit()

    async def run_action(self, action, staff, order_id):
        async with self.Session() as session:
            return await action(session, staff, order_id, actor="admin:test")

    async def state(self):
        async with self.Session() as session:
            orders = {o.id: o.status for o in (await session.scalars(select(Order))).all()}
            balance = (await session.get(BotUser, 1)).wallet_balance
            shop_wallet = await session.scalar(select(ShopWallet.balance).where(ShopWallet.user_id == 6, ShopWallet.reseller_id == 5))
            txs = len((await session.scalars(select(WalletTransaction))).all())
            open_failures = len((await session.scalars(select(DeliveryFailure).where(DeliveryFailure.resolved_at.is_(None)))).all())
        return orders, balance, shop_wallet, txs, open_failures


class RefundTests(StuckCase):
    async def test_the_paid_amount_goes_back_to_the_wallet_and_the_order_is_closed(self):
        result = await self.run_action(cancel_and_refund, OWNER, 10)
        self.assertEqual((result.refunded, result.user_id), (50000, 1))
        orders, balance, _shop, txs, failures = await self.state()
        self.assertEqual((orders[10], balance, txs, failures), ("cancelled", 51000, 1, 0))
        async with self.Session() as session:
            note = (await session.scalars(select(Payment.review_note).where(Payment.order_id == 10, Payment.status == "approved"))).one()
        self.assertIn("refunded 50000", note)
        self.assertIn("admin:test", note)

    async def test_a_second_press_refunds_nothing_more(self):
        await self.run_action(cancel_and_refund, OWNER, 10)
        with self.assertRaises(ValueError) as caught:
            await self.run_action(cancel_and_refund, OWNER, 10)
        self.assertEqual(str(caught.exception), NOT_STUCK)
        self.assertEqual((await self.state())[1:4], (51000, None, 1))

    async def test_simultaneous_presses_refund_once(self):
        results = await asyncio.gather(
            self.run_action(cancel_and_refund, OWNER, 10), self.run_action(cancel_and_refund, OWNER, 10), return_exceptions=True
        )
        self.assertEqual(sum(1 for r in results if not isinstance(r, BaseException)), 1)
        self.assertEqual((await self.state())[1], 51000)

    async def test_a_shop_order_is_refunded_to_that_shops_own_purse_only(self):
        await self.run_action(cancel_and_refund, SHOP, 11)
        orders, balance, shop_wallet, _txs, _f = await self.state()
        self.assertEqual((orders[11], shop_wallet, balance), ("cancelled", 30000, 1000))

    async def test_nobody_acts_on_an_order_of_another_shop_or_the_platform(self):
        for staff, order_id in ((SHOP, 10), (OWNER, 11), ({"role": "reseller", "bot_user_id": 9}, 11)):
            with self.subTest(order=order_id), self.assertRaises(ShopScopeError):
                await self.run_action(cancel_and_refund, staff, order_id)
            with self.assertRaises(ShopScopeError):
                await self.run_action(mark_delivered_manually, staff, order_id)
        orders, balance, shop_wallet, txs, _f = await self.state()
        self.assertEqual((orders[10], orders[11], balance, shop_wallet, txs), ("paid", "paid", 1000, None, 0))

    async def test_an_order_touched_a_moment_ago_may_still_be_delivering(self):
        with self.assertRaises(ValueError):
            await self.run_action(cancel_and_refund, OWNER, 12)
        self.assertEqual((await self.state())[0][12], "paid")

    async def test_other_states_and_missing_orders_are_refused(self):
        for order_id in (13, 999):
            with self.subTest(order=order_id), self.assertRaises(ValueError):
                await self.run_action(cancel_and_refund, OWNER, order_id)
        self.assertEqual((await self.state())[1:4], (1000, None, 0))

    async def test_a_renewal_that_never_reached_the_panel_is_refunded(self):
        result = await self.run_action(cancel_and_refund, OWNER, 14)
        self.assertEqual(result.refunded, 4000)
        self.assertEqual((await self.state())[0][14], "cancelled")

    async def test_a_renewal_with_an_unknown_panel_outcome_is_not_refunded(self):
        with self.assertRaises(ValueError) as ctx:
            await self.run_action(cancel_and_refund, OWNER, 16)
        self.assertEqual(str(ctx.exception), MUTATION_MAY_BE_APPLIED)
        orders, balance, _s, txs, _f = await self.state()
        self.assertEqual((orders[16], balance, txs), ("paid", 1000, 0))

    async def test_an_application_order_is_refunded_and_its_application_closed(self):
        result = await self.run_action(cancel_and_refund, OWNER, 17)
        self.assertEqual(result.refunded, 8000)
        async with self.Session() as session:
            self.assertEqual((await session.get(ResellerApplication, 1)).status, "cancelled")

    async def test_an_approved_application_is_not_refunded(self):
        with self.assertRaises(ValueError) as ctx:
            await self.run_action(cancel_and_refund, OWNER, 18)
        self.assertEqual(str(ctx.exception), APPLICATION_APPROVED)
        self.assertEqual((await self.state())[1], 1000)

    async def test_an_order_with_nothing_paid_is_closed_without_a_credit(self):
        result = await self.run_action(cancel_and_refund, OWNER, 15)
        self.assertEqual(result.refunded, 0)
        self.assertEqual((await self.state())[3], 0)

    async def test_only_approved_payments_are_returned(self):
        result = await self.run_action(cancel_and_refund, OWNER, 10)
        self.assertEqual(result.refunded, 50000)


class ManualDeliveryTests(StuckCase):
    async def test_the_order_is_closed_without_moving_money_and_only_once(self):
        result = await self.run_action(mark_delivered_manually, OWNER, 10)
        self.assertEqual(result.refunded, 0)
        orders, balance, _shop, txs, failures = await self.state()
        self.assertEqual((orders[10], balance, txs, failures), ("delivered", 1000, 0, 0))
        with self.assertRaises(ValueError):
            await self.run_action(mark_delivered_manually, OWNER, 10)

    async def test_the_two_actions_exclude_each_other(self):
        await self.run_action(mark_delivered_manually, OWNER, 10)
        with self.assertRaises(ValueError):
            await self.run_action(cancel_and_refund, OWNER, 10)
        self.assertEqual((await self.state())[1], 1000)


class CloseWithoutRefundTests(StuckCase):
    async def test_any_stuck_order_is_closed_once_without_moving_money(self):
        for order_id in (10, 14, 16, 17, 18):
            result = await self.run_action(close_without_refund, OWNER, order_id)
            self.assertEqual(result.refunded, 0)
        orders, balance, _s, txs, failures = await self.state()
        self.assertEqual([orders[i] for i in (10, 14, 16, 17, 18)], ["cancelled"] * 5)
        self.assertEqual((balance, txs, failures), (1000, 0, 0))
        with self.assertRaises(ValueError):
            await self.run_action(close_without_refund, OWNER, 10)
        async with self.Session() as session:
            self.assertFalse((await session.get(Order, 16)).service_mutation_pending)
            self.assertEqual((await session.get(ResellerApplication, 1)).status, "cancelled")
            self.assertEqual((await session.get(ResellerApplication, 2)).status, "approved")

    async def test_another_shop_cannot_close_it(self):
        with self.assertRaises(ShopScopeError):
            await self.run_action(close_without_refund, SHOP, 10)
        self.assertEqual((await self.state())[0][10], "paid")


class ReasonTests(StuckCase):
    async def test_each_kind_gets_its_own_reason_and_refund_flag(self):
        async with self.Session() as session:
            orders = list((await session.scalars(select(Order).where(Order.id.in_([10, 14, 15, 16, 17, 18])))).all())
            reasons = await describe_stuck_orders(session, orders)
        self.assertEqual(reasons[10].kind, "خرید سرویس")
        self.assertIn("ساخته نشد", reasons[10].reason)
        self.assertIn("تمام نشد", reasons[15].reason)
        self.assertEqual(reasons[14].kind, "تمدید")
        self.assertIn("دیگر وجود ندارد", reasons[14].reason)
        self.assertTrue(reasons[14].can_refund)
        self.assertIn("نامعلوم", reasons[16].reason)
        self.assertFalse(reasons[16].can_refund)
        self.assertEqual(reasons[17].kind, "درخواست نمایندگی")
        self.assertIn("منتظر تأیید", reasons[17].reason)
        self.assertFalse(reasons[18].can_refund)


class StillStuckTests(StuckCase):
    async def test_counts_only_paid_orders_without_a_service(self):
        async with self.Session() as session:
            self.assertEqual(await count_still_stuck(session, [10, 11, 13, 15]), 3)
            self.assertEqual(await count_still_stuck(session, []), 0)


class RouteTests(StuckCase):
    async def post(self, payload, path):
        cookie = f"session={self.signer.dumps(payload)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.post(path, headers=headers, data={})

    async def test_the_refund_route_refunds_tells_the_customer_once_and_reports(self):
        with patch("app.api.delivery_failure_pages.notify_payer", AsyncMock(return_value=True)) as notify:
            response = await self.post(OWNER_SESSION, "/orders/10/refund-stuck-order")
            again = await self.post(OWNER_SESSION, "/orders/10/refund-stuck-order")
        self.assertIn("ok=", response.headers["location"])
        self.assertIn("err=", again.headers["location"])
        notify.assert_awaited_once()
        self.assertEqual(notify.await_args.args[1], 1)
        self.assertEqual((await self.state())[1], 51000)

    async def test_a_shop_cannot_use_the_routes_on_a_platform_order(self):
        for path in ("/orders/10/refund-stuck-order", "/orders/10/mark-delivered-manually"):
            response = await self.post(SHOP_SESSION, path)
            self.assertIn("err=", response.headers["location"], path)
        self.assertEqual((await self.state())[0][10], "paid")

    async def test_the_manual_delivery_route_closes_the_order(self):
        response = await self.post(OWNER_SESSION, "/orders/10/mark-delivered-manually")
        self.assertIn("ok=", response.headers["location"])
        self.assertEqual((await self.state())[0][10], "delivered")

    async def test_removing_the_error_row_of_a_stuck_order_says_it_stays_in_the_list(self):
        response = await self.post(OWNER_SESSION, "/orders/10/dismiss-delivery-failure")
        self.assertIn("ok=", response.headers["location"])
        from urllib.parse import parse_qs, urlsplit

        message = parse_qs(urlsplit(response.headers["location"]).query)["ok"][0]
        self.assertIn("هنوز", message)
        self.assertIn("گیرکرده", message)

    async def test_the_list_offers_the_close_actions_for_a_stuck_order_only(self):
        cookie = f"session={self.signer.dumps(OWNER_SESSION)}; csrf=token"
        page = await self.client.get("/finance?tab=delivery", headers={"cookie": cookie})
        self.assertEqual(page.status_code, 200)
        for path in ("refund-stuck-order", "mark-delivered-manually", "close-stuck-order"):
            self.assertIn(f"/orders/10/{path}", page.text)
            self.assertNotIn(f"/orders/13/{path}", page.text)
        # A renewal that may already be applied is offered no refund, only the other two.
        self.assertNotIn("/orders/16/refund-stuck-order", page.text)
        self.assertIn("/orders/16/close-stuck-order", page.text)
        self.assertIn("دیگر وجود ندارد", page.text)

    async def test_the_close_route_closes_the_order(self):
        response = await self.post(OWNER_SESSION, "/orders/14/close-stuck-order")
        self.assertIn("ok=", response.headers["location"])
        self.assertEqual((await self.state())[0][14], "cancelled")
